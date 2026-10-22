"""Typed op kernel: one atomic call, many typed ops, one snapshot.

An op list is an ORDERED LIST OF TYPED OPS (:data:`Op`), a discriminated union over the direct-DOM
engines (``edit.create`` / ``style`` / ``text_object`` / ``transform`` / ``compose`` /
``collection`` / ``optimize``) plus the Inkscape-backed path ops. It replaces the former
``edit/batch.py`` and adds what a one-shot "draw it" call needs:

* **targets** — every op that acts on existing objects takes ``ids`` (explicit) OR ``select`` (a
  :class:`Selector`, the ``find_in_root`` predicates). A selector is resolved when its op RUNS,
  against the in-memory tree, so it also matches objects created by earlier ops of the same call;
* **references** — ``"$<n>"`` in an id field (and ``url(#$<n>)`` in a paint field) names the id the
  n-th op of the same call created, so a gradient can be created and used, or a group created and
  filled, without a round trip;
* **atomicity** — the ops run in order against ONE in-memory tree handed to
  :func:`inkscape_mcp.edit.pipeline.apply_edit`, which writes only after every op succeeded. Any
  op failing is a no-op for the whole call (the document stays byte-identical), and the call lands
  under ONE snapshot + ONE Operation Record, so one ``restore`` reverts everything (ADR-004);
* **risk** — the call's risk is the MAX over its ops (:func:`ops_risk`): one HIGH op (``delete``,
  ``set_svg``, ``insert_svg``, the path ops) escalates the call and demands an ``approval_token``;
* **dry run** — :func:`run_ops` with ``dry_run=True`` runs the ops on a throwaway copy of the tree
  and returns the plan (matched ids, predicted ids, risk) without opening a record or writing
  anything. Ops that need the Inkscape engine (path ops, ``fit_to_content``) are only described.

SECURITY (sec.12): every value still flows through the same ``edit.dom`` / ``style`` / ``create``
validators the single-purpose tools used — object ids are used only for in-tree lookup (never argv,
except the path ops, which re-validate them), and no value reaches an attribute without its
normalizer. The kernel adds no new authority: it is N existing typed edits under one record.
"""

from __future__ import annotations

import copy
import difflib
import re
import uuid
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self

from fastmcp.exceptions import ToolError
from lxml import etree
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from inkscape_mcp.config import get_settings
from inkscape_mcp.document.inspect import (
    BBox,
    DocumentNotFound,
    InspectionError,
    _engine_bboxes,
    _load_tree,
    bbox_of,
    find_in_root,
)
from inkscape_mcp.edit import collection as collection_engine
from inkscape_mcp.edit import compose as compose_engine
from inkscape_mcp.edit import create as create_engine
from inkscape_mcp.edit import optimize as optimize_engine
from inkscape_mcp.edit import paths as paths_engine
from inkscape_mcp.edit import style as style_engine
from inkscape_mcp.edit import text_object as text_engine
from inkscape_mcp.edit import transform as transform_engine
from inkscape_mcp.edit.dom import (
    EditError,
    TargetNotFound,
    all_ids,
    find_by_id,
    load_working_tree,
)
from inkscape_mcp.edit.pipeline import EditApplyError, EditResult, apply_edit
from inkscape_mcp.logging_setup import get_logger
from inkscape_mcp.registry import DocEntry, Registry, get_registry
from inkscape_mcp.render.cli import RenderError, render_preview
from inkscape_mcp.validation_errors import format_validation_errors
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.limits import LimitExceeded, check_input_bytes_size
from inkscape_mcp.workspace.risk import RiskClass
from inkscape_mcp.workspace.subprocess_exec import ProcessError

_logger = get_logger("edit.ops")

#: Hard cap on the number of ops in one call. A call is a deliberate composition, not an unbounded
#: program: this bounds the in-memory work, the Operation Record size, and the blast radius of one
#: approval.
MAX_OPS = 64

#: Default cap on the objects one selector may match (an over-broad selector rejects the op BEFORE
#: it mutates anything — a caller may raise it, but never silently fans out across the document).
DEFAULT_MAX_MATCHES = 64

#: Total ordering over the risk vocabulary so a call's effective risk is the MAX over its ops.
_RISK_ORDER: dict[RiskClass, int] = {
    RiskClass.LOW: 0,
    RiskClass.MEDIUM: 1,
    RiskClass.HIGH: 2,
    RiskClass.RESTRICTED: 3,
}

_REF_RE = re.compile(r"^\$(\d+)$")
_URL_REF_RE = re.compile(r"url\(\s*#\$(\d+)\s*\)")

#: Errors an op may raise that are reported as `ops[i] <op>: <message>` (all carry stable,
#: host-path-free messages).
_OP_FAILURES = (
    EditError,
    paths_engine.PathOpError,
    transform_engine.ContentBBoxError,
    LimitExceeded,
    ToolError,
    DocumentNotFound,
    InspectionError,
    ValueError,
)

#: A failed best-effort render never aborts the (already applied) edit.
_RENDER_FAILURES = (RenderError, ProcessError, LimitExceeded)


class OpError(EditError):
    """An op list was invalid or an op failed; the message names the op (`ops[i] <op>: …`).

    A subclass of :class:`EditError` so the pipeline discards the Operation Record exactly as it
    does for any other edit error. `op_index` is the failing op's position (None for a list-level
    problem such as an empty or over-long list).
    """

    def __init__(self, message: str, *, op_index: int | None = None) -> None:
        super().__init__(message)
        self.op_index = op_index


class Selector(BaseModel):
    """Select objects by predicate — the same filters `find_objects` takes (AND semantics).

    `tag` is an exact local element name; `fill` / `stroke` match the EFFECTIVE cascade-resolved
    paint; `text` is a case-insensitive substring; `id_prefix` an id prefix; `bbox` an intersection
    box. `accurate_bbox` opts into geometry-accurate boxes (one read-only Inkscape query) so
    transformed / path / text objects can match a `bbox`. More than `max_matches` matches rejects
    the op.
    """

    model_config = ConfigDict(extra="forbid")

    tag: str | None = None
    fill: str | None = None
    stroke: str | None = None
    text: str | None = None
    id_prefix: str | None = None
    bbox: BBox | None = None
    accurate_bbox: bool = False
    max_matches: int = Field(default=DEFAULT_MAX_MATCHES, ge=1, le=1024)


class Stop(BaseModel):
    """One gradient stop: `offset` 0..1 (or 0%..100%), `color`, optional `opacity` 0..1."""

    model_config = ConfigDict(extra="forbid")

    offset: float | str
    color: str
    opacity: float | None = None


class EditContext:
    """Per-call helpers that need the document's sandbox, not just its tree.

    `materialize(tree)` writes the CURRENT in-memory tree to a sandboxed temp SVG so an
    Inkscape-backed op (path ops, an accurate-bbox selector, `fit_to_content`'s fallback) sees what
    earlier ops of the same call did instead of the stale on-disk working copy. The registry entry
    is looked up lazily so "document id not found" keeps coming from `apply_edit`. `cleanup`
    removes every temp file.
    """

    def __init__(self, doc_id: str, registry: Registry | None = None) -> None:
        self.doc_id = doc_id
        self._registry = registry
        self._files: list[Path] = []
        self._dir: Path | None = None

    def _entry(self) -> DocEntry:
        reg = self._registry if self._registry is not None else get_registry()
        try:
            return reg.get(self.doc_id)
        except KeyError:
            raise DocumentNotFound("document id not found") from None

    def materialize(self, tree: etree._ElementTree) -> Path:
        """Write `tree` to a fresh sandboxed temp SVG and return its path."""
        entry = self._entry()
        tmp_dir = sandbox.artifacts_dir(Path(entry.root), self.doc_id) / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        self._dir = tmp_dir
        path = tmp_dir / f"kernel-{uuid.uuid4().hex}.svg"
        path.write_bytes(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
        self._files.append(path)
        return path

    def cleanup(self) -> None:
        """Remove every materialized file (best effort) and the temp dir when it is empty."""
        for path in self._files:
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best-effort cleanup
                pass
        self._files.clear()
        if self._dir is not None:
            try:
                self._dir.rmdir()
            except OSError:
                pass


# --- results ----------------------------------------------------------------


class CreatedRef(BaseModel):
    """An id an op created: op index, id, and its attribute-derived box (None if unknown)."""

    op: int
    id: str
    bbox: list[float] | None = None


class OpOutcome(BaseModel):
    """What one op did (or, in a dry run, would do): its summary, matched targets, new ids."""

    op: int
    name: str
    summary: str
    matched: list[str] | None = None
    created_ids: list[str] = Field(default_factory=list)


class OpsResult(BaseModel):
    """Outcome of one :func:`run_ops` call.

    `changed` is False for a dry run and for a call whose ops changed nothing (then `operation_id`
    and `snapshot_id` are empty). `outcomes` is the per-op plan/result; `created` lists the id each
    creating op produced (referenceable as `$<op>` by later ops of the same call). `render_artifact`
    is the workspace-root-relative PNG when `render=True` produced one.
    """

    doc_id: str
    changed: bool
    dry_run: bool = False
    operation_id: str = ""
    snapshot_id: str = ""
    risk: str
    needs_approval: bool = False
    summary: str = ""
    created: list[CreatedRef] = Field(default_factory=list)
    outcomes: list[OpOutcome] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    render_artifact: str | None = None


# --- the run state ----------------------------------------------------------


def _ordered_ids(tree: etree._ElementTree) -> list[str]:
    """Every `id` in document order."""
    out: list[str] = []
    for elem in tree.getroot().iter():
        if isinstance(elem.tag, str):
            oid = elem.get("id")
            if oid:
                out.append(oid)
    return out


def _similar(oid: str, present: set[str]) -> str:
    matches = difflib.get_close_matches(oid, sorted(present), n=3, cutoff=0.6)
    return f" (similar: {', '.join(matches)})" if matches else ""


class _Run:
    """Mutable state of one kernel run (real or dry): the tree, ids created so far, outcomes."""

    def __init__(self, ops: list[Op], ctx: EditContext, *, dry_run: bool) -> None:
        self.ops = ops
        self.ctx = ctx
        self.dry_run = dry_run
        self.tree: etree._ElementTree
        self.index = 0
        #: the id op `i` created (the target of a `$i` reference)
        self.primary: dict[int, str] = {}
        #: set by an op to name the id it produced when a before/after id diff cannot
        self.override: str | None = None
        self.matched: list[str] | None = None
        self.outcomes: list[OpOutcome] = []
        self.created: list[CreatedRef] = []
        self.warnings: list[str] = []

    # --- helpers the ops use ---

    def fail(self, message: str) -> OpError:
        return OpError(
            f"ops[{self.index}] {self.ops[self.index].op}: {message}", op_index=self.index
        )

    def targets(self, op: _Targeted) -> list[str]:
        """Resolve `op`'s `ids` / `select` against the CURRENT in-memory tree."""
        if op.ids is not None:
            if not op.ids:
                raise self.fail("'ids' is empty")
            self.matched = list(op.ids)
            return list(op.ids)
        if op.select is None:
            raise self.fail("give 'ids' or 'select'")
        sel = op.select
        engine_boxes = (
            _engine_bboxes(str(self.ctx.materialize(self.tree))) if sel.accurate_bbox else None
        )
        refs = find_in_root(
            self.tree.getroot(),
            tag=sel.tag,
            fill=sel.fill,
            stroke=sel.stroke,
            text=sel.text,
            id_prefix=sel.id_prefix,
            bbox=sel.bbox,
            engine_boxes=engine_boxes,
        )
        ids = [r.object_id for r in refs]
        if not ids:
            raise self.fail("select matched 0 objects")
        if len(ids) > sel.max_matches:
            raise self.fail(
                f"select matched {len(ids)} objects, more than max_matches={sel.max_matches}; "
                "narrow the selector or raise max_matches"
            )
        self.matched = ids
        return ids

    def optional_scope(self, op: _Targeted) -> list[str] | None:
        """`ids` / `select` of an op whose scope is optional (None = whole document)."""
        if op.ids is None and op.select is None:
            return None
        return self.targets(op)

    def each(self, op: _Targeted, fn: Any) -> str:
        """Run a single-target engine function once per resolved target."""
        return "; ".join(fn(oid) for oid in self.targets(op))

    # --- reference / id resolution ---

    def _ref(self, token: str) -> str:
        k = int(token)
        oid = self.primary.get(k)
        if oid is None:
            raise self.fail(f"${k} refers to op {k} ({self.ops[k].op}) which created no id")
        return oid

    def _sub_id(self, value: str) -> str:
        m = _REF_RE.match(value)
        return self._ref(m.group(1)) if m else value

    def _sub_paint(self, value: str) -> str:
        return _URL_REF_RE.sub(lambda m: f"url(#{self._ref(m.group(1))})", value)

    def resolve(self, op: _Op) -> _Op:
        """Substitute `$n` references and verify every referenced existing id is present."""
        changes: dict[str, Any] = {}
        present: set[str] | None = None
        for name in op.ID_FIELDS:
            value = getattr(op, name)
            if value is None:
                continue
            ids = [value] if isinstance(value, str) else list(value)
            ids = [self._sub_id(v) for v in ids]
            if present is None:
                present = all_ids(self.tree.getroot())
            for oid in ids:
                if oid not in present:
                    label = "" if name == "ids" else f"{name} "
                    raise self.fail(f"{label}id {oid!r} not found{_similar(oid, present)}")
            changes[name] = ids[0] if isinstance(value, str) else ids
        for name in op.PAINT_FIELDS:
            value = getattr(op, name)
            if isinstance(value, str):
                changes[name] = self._sub_paint(value)
        return op.model_copy(update=changes) if changes else op

    # --- the loop ---

    def execute(self, tree: etree._ElementTree) -> str:
        """Run every op in order against `tree` (the pipeline's `mutate`)."""
        self.tree = tree
        summaries: list[str] = []
        for index, op in enumerate(self.ops):
            self.index = index
            self.override = None
            self.matched = None
            before_ids = _ordered_ids(tree)
            try:
                summary = self.resolve(op).apply(self)
            except OpError:
                raise
            except TargetNotFound as exc:
                raise self.fail("object id not found in document") from exc
            except _OP_FAILURES as exc:
                raise self.fail(str(exc)) from exc
            self._record(op, summary, before_ids)
            summaries.append(f"{op.op}: {summary}")
        return f"applied {len(self.ops)} op(s) | " + " | ".join(summaries)

    def _record(self, op: _Op, summary: str, before_ids: list[str]) -> None:
        new_ids: list[str] = []
        if op.CAPTURE:
            known = set(before_ids)
            new_ids = [i for i in _ordered_ids(self.tree) if i not in known]
        primary = self.override or (new_ids[0] if new_ids and op.CREATES else None)
        if primary is not None:
            self.primary[self.index] = primary
            elem = find_by_id(self.tree.getroot(), primary)
            box = bbox_of(elem) if elem is not None else None
            self.created.append(
                CreatedRef(
                    op=self.index,
                    id=primary,
                    bbox=[box.x, box.y, box.width, box.height] if box is not None else None,
                )
            )
        self.outcomes.append(
            OpOutcome(
                op=self.index,
                name=op.op,
                summary=summary,
                matched=self.matched,
                created_ids=new_ids if op.CREATES else [],
            )
        )


# --- op base classes --------------------------------------------------------


class _Op(BaseModel):
    """Base for one typed op. Subclasses carry a `Literal` `op` tag and an :meth:`apply`."""

    model_config = ConfigDict(extra="forbid")

    #: Risk of this op (escalated to the call max by :func:`ops_risk`).
    RISK: ClassVar[RiskClass] = RiskClass.MEDIUM

    def effective_risk(self) -> RiskClass:
        """This instance's risk; an op whose fields raise it overrides this."""
        return self.RISK

    #: Fields holding ids of EXISTING objects (verified present; `$n` allowed).
    ID_FIELDS: ClassVar[tuple[str, ...]] = ()
    #: Fields holding a paint (`url(#$n)` allowed).
    PAINT_FIELDS: ClassVar[tuple[str, ...]] = ()
    #: The op produces an id later ops may reference as `$<index>`.
    CREATES: ClassVar[bool] = False
    #: Report ids that appear because of this op (False where the engine re-ids the whole tree).
    CAPTURE: ClassVar[bool] = True
    #: Needs the Inkscape engine (only described in a dry run).
    ENGINE: ClassVar[bool] = False
    #: Only available when `raw_action_enabled` is on.
    RAW_ACTION: ClassVar[bool] = False

    op: str

    def apply(self, run: _Run) -> str:  # pragma: no cover - overridden by every concrete op
        raise NotImplementedError


class _Targeted(_Op):
    """An op that acts on existing objects: `ids` (explicit) or `select` (a :class:`Selector`)."""

    ID_FIELDS: ClassVar[tuple[str, ...]] = ("ids",)
    #: False = the scope is optional (no target means the whole document).
    TARGET_REQUIRED: ClassVar[bool] = True

    ids: list[str] | None = None
    select: Selector | None = None

    @model_validator(mode="after")
    def _check_target(self) -> Self:
        given = (self.ids is not None) + (self.select is not None)
        if self.TARGET_REQUIRED and given != 1:
            raise ValueError("give exactly one of 'ids' or 'select'")
        if given > 1:
            raise ValueError("give at most one of 'ids' or 'select'")
        return self


class _Create(_Op):
    """A creating op: optional new `id` and `parent` (default: the first layer, else the root)."""

    ID_FIELDS: ClassVar[tuple[str, ...]] = ("parent",)
    CREATES: ClassVar[bool] = True

    id: str | None = None
    parent: str | None = None


# --- create -----------------------------------------------------------------


class CreateRect(_Create):
    """Create a `<rect>`."""

    PAINT_FIELDS: ClassVar[tuple[str, ...]] = ("fill", "stroke")

    op: Literal["create_rect"]
    x: float
    y: float
    width: float
    height: float
    rx: float | None = None
    ry: float | None = None
    fill: str | None = None
    stroke: str | None = None
    stroke_width: str | None = None

    def apply(self, run: _Run) -> str:
        return create_engine.make_create_rect(
            self.x,
            self.y,
            self.width,
            self.height,
            parent_id=self.parent,
            object_id=self.id,
            rx=self.rx,
            ry=self.ry,
            fill=self.fill,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
        )(run.tree)


class CreateCircle(_Create):
    """Create a `<circle>`."""

    PAINT_FIELDS: ClassVar[tuple[str, ...]] = ("fill", "stroke")

    op: Literal["create_circle"]
    cx: float
    cy: float
    r: float
    fill: str | None = None
    stroke: str | None = None
    stroke_width: str | None = None

    def apply(self, run: _Run) -> str:
        return create_engine.make_create_circle(
            self.cx,
            self.cy,
            self.r,
            parent_id=self.parent,
            object_id=self.id,
            fill=self.fill,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
        )(run.tree)


class CreateEllipse(_Create):
    """Create an `<ellipse>`."""

    PAINT_FIELDS: ClassVar[tuple[str, ...]] = ("fill", "stroke")

    op: Literal["create_ellipse"]
    cx: float
    cy: float
    rx: float
    ry: float
    fill: str | None = None
    stroke: str | None = None
    stroke_width: str | None = None

    def apply(self, run: _Run) -> str:
        return create_engine.make_create_ellipse(
            self.cx,
            self.cy,
            self.rx,
            self.ry,
            parent_id=self.parent,
            object_id=self.id,
            fill=self.fill,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
        )(run.tree)


class CreateLine(_Create):
    """Create a `<line>`."""

    PAINT_FIELDS: ClassVar[tuple[str, ...]] = ("stroke",)

    op: Literal["create_line"]
    x1: float
    y1: float
    x2: float
    y2: float
    stroke: str | None = None
    stroke_width: str | None = None

    def apply(self, run: _Run) -> str:
        return create_engine.make_create_line(
            self.x1,
            self.y1,
            self.x2,
            self.y2,
            parent_id=self.parent,
            object_id=self.id,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
        )(run.tree)


class CreatePolygon(_Create):
    """Create a closed `<polygon>` from `[x, y]` points."""

    PAINT_FIELDS: ClassVar[tuple[str, ...]] = ("fill", "stroke")

    op: Literal["create_polygon"]
    points: list[tuple[float, float]]
    fill: str | None = None
    stroke: str | None = None
    stroke_width: str | None = None

    def apply(self, run: _Run) -> str:
        return create_engine.make_create_polygon(
            self.points,
            parent_id=self.parent,
            object_id=self.id,
            fill=self.fill,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
        )(run.tree)


class CreatePolyline(_Create):
    """Create an open `<polyline>` from `[x, y]` points."""

    PAINT_FIELDS: ClassVar[tuple[str, ...]] = ("fill", "stroke")

    op: Literal["create_polyline"]
    points: list[tuple[float, float]]
    fill: str | None = None
    stroke: str | None = None
    stroke_width: str | None = None

    def apply(self, run: _Run) -> str:
        return create_engine.make_create_polyline(
            self.points,
            parent_id=self.parent,
            object_id=self.id,
            fill=self.fill,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
        )(run.tree)


class CreatePath(_Create):
    """Create a `<path>` from path data `d`."""

    PAINT_FIELDS: ClassVar[tuple[str, ...]] = ("fill", "stroke")

    op: Literal["create_path"]
    d: str
    fill: str | None = None
    stroke: str | None = None
    stroke_width: str | None = None

    def apply(self, run: _Run) -> str:
        return create_engine.make_create_path(
            self.d,
            parent_id=self.parent,
            object_id=self.id,
            fill=self.fill,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
        )(run.tree)


class CreateText(_Create):
    """Create a `<text>` at `(x, y)`; `anchor='middle'` centres it on `x`."""

    PAINT_FIELDS: ClassVar[tuple[str, ...]] = ("fill", "stroke")

    op: Literal["create_text"]
    x: float
    y: float
    text: str
    fill: str | None = None
    stroke: str | None = None
    stroke_width: str | None = None
    anchor: Literal["start", "middle", "end"] | None = None

    def apply(self, run: _Run) -> str:
        return create_engine.make_create_text(
            self.x,
            self.y,
            self.text,
            parent_id=self.parent,
            object_id=self.id,
            fill=self.fill,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
            anchor=self.anchor,
        )(run.tree)


class CreateGroup(_Create):
    """Create an empty `<g>`."""

    op: Literal["create_group"]

    def apply(self, run: _Run) -> str:
        return create_engine.make_create_group(parent_id=self.parent, object_id=self.id)(run.tree)


class CreateUse(_Create):
    """Create a `<use>` instance of the existing object `href`."""

    ID_FIELDS: ClassVar[tuple[str, ...]] = ("href", "parent")

    op: Literal["create_use"]
    href: str
    x: float | None = None
    y: float | None = None
    transform: str | None = None

    def apply(self, run: _Run) -> str:
        return create_engine.make_create_use(
            self.href,
            parent_id=self.parent,
            object_id=self.id,
            x=self.x,
            y=self.y,
            transform=self.transform,
        )(run.tree)


class LinearGradient(_Op):
    """Add a `<linearGradient>` to `<defs>`; use it as `url(#id)` or `url(#$n)` in a later paint."""

    CREATES: ClassVar[bool] = True

    op: Literal["linear_gradient"]
    stops: list[Stop]
    x1: str = "0%"
    y1: str = "0%"
    x2: str = "100%"
    y2: str = "0%"
    id: str | None = None

    def apply(self, run: _Run) -> str:
        return create_engine.make_add_linear_gradient(
            [s.model_dump(exclude_none=True) for s in self.stops],
            x1=self.x1,
            y1=self.y1,
            x2=self.x2,
            y2=self.y2,
            object_id=self.id,
        )(run.tree)


class RadialGradient(_Op):
    """Add a `<radialGradient>` to `<defs>`; use it as `url(#id)` or `url(#$n)` in a later paint."""

    CREATES: ClassVar[bool] = True

    op: Literal["radial_gradient"]
    stops: list[Stop]
    cx: str = "50%"
    cy: str = "50%"
    r: str = "50%"
    fx: str | None = None
    fy: str | None = None
    id: str | None = None

    def apply(self, run: _Run) -> str:
        return create_engine.make_add_radial_gradient(
            [s.model_dump(exclude_none=True) for s in self.stops],
            cx=self.cx,
            cy=self.cy,
            r=self.r,
            fx=self.fx,
            fy=self.fy,
            object_id=self.id,
        )(run.tree)


class Group(_Targeted):
    """Wrap the targets in a new `<g>` (the new group's id is `id`, or minted)."""

    CREATES: ClassVar[bool] = True

    op: Literal["group"]
    id: str | None = None

    def apply(self, run: _Run) -> str:
        return create_engine.make_group_objects(run.targets(self), object_id=self.id)(run.tree)


class Reparent(_Targeted):
    """Move each target under the existing object `parent` (appended last, so painted on top)."""

    ID_FIELDS: ClassVar[tuple[str, ...]] = ("ids", "parent")

    op: Literal["reparent"]
    parent: str

    def apply(self, run: _Run) -> str:
        parent = self.parent
        return run.each(self, lambda oid: create_engine.make_reparent_object(oid, parent)(run.tree))


# --- style ------------------------------------------------------------------


class Fill(_Targeted):
    """Set the fill colour (or `url(#id)`) of the targets, optionally with a fill opacity."""

    PAINT_FIELDS: ClassVar[tuple[str, ...]] = ("color",)

    op: Literal["fill"]
    color: str
    opacity: float | None = None

    def apply(self, run: _Run) -> str:
        return style_engine.set_fill_mutate(run.targets(self), self.color, self.opacity)(run.tree)


class Stroke(_Targeted):
    """Set stroke colour / width / opacity of the targets (at least one of the three)."""

    PAINT_FIELDS: ClassVar[tuple[str, ...]] = ("color",)

    op: Literal["stroke"]
    color: str | None = None
    width: str | None = None
    opacity: float | None = None

    def apply(self, run: _Run) -> str:
        return style_engine.set_stroke_mutate(
            run.targets(self), self.color, self.width, self.opacity
        )(run.tree)


class Opacity(_Targeted):
    """Set the element-level opacity (0..1) of the targets."""

    op: Literal["opacity"]
    opacity: float

    def apply(self, run: _Run) -> str:
        return style_engine.set_opacity_mutate(run.targets(self), self.opacity)(run.tree)


class ReplaceColor(_Targeted):
    """Replace colour `from_color` by `to_color` document-wide, or within `ids` / `select`."""

    TARGET_REQUIRED: ClassVar[bool] = False

    op: Literal["replace_color"]
    from_color: str
    to_color: str

    def apply(self, run: _Run) -> str:
        return style_engine.replace_color_mutate(
            self.from_color, self.to_color, run.optional_scope(self)
        )(run.tree)


class Palette(_Targeted):
    """Apply many colour replacements (`mapping` old -> new), document-wide or within a scope."""

    TARGET_REQUIRED: ClassVar[bool] = False

    op: Literal["palette"]
    mapping: dict[str, str]

    def apply(self, run: _Run) -> str:
        return style_engine.apply_palette_mutate(self.mapping, run.optional_scope(self))(run.tree)


class Font(_Targeted):
    """Set font family / size / weight (at least one) on text targets."""

    op: Literal["font"]
    family: str | None = None
    size: str | None = None
    weight: str | None = None

    def apply(self, run: _Run) -> str:
        ids = run.targets(self)
        summary = text_engine.make_set_font(ids, self.family, self.size, self.weight)(run.tree)
        if self.family is not None:
            _ok, details = text_engine.font_coverage_for_root(run.tree.getroot(), ids)
            for d in details:
                if d.uncovered_chars:
                    hint = f"; try {d.suggested_family!r}" if d.suggested_family else ""
                    run.warnings.append(
                        f"ops[{run.index}] font: {d.family!r} cannot render "
                        f"{d.uncovered_chars!r} in {d.object_id!r}{hint}"
                    )
        return summary


# --- object -----------------------------------------------------------------


class ReplaceText(_Targeted):
    """Replace the text content of text targets."""

    op: Literal["replace_text"]
    text: str

    def apply(self, run: _Run) -> str:
        text = self.text
        return run.each(self, lambda oid: text_engine.make_replace_text(oid, text)(run.tree))


class Duplicate(_Targeted):
    """Duplicate each target in place (clone stacked just above). `new_id` needs one target."""

    CREATES: ClassVar[bool] = True

    op: Literal["duplicate"]
    new_id: str | None = None

    def apply(self, run: _Run) -> str:
        ids = run.targets(self)
        if self.new_id is not None and len(ids) != 1:
            raise run.fail(f"'new_id' needs exactly one target, got {len(ids)}")
        new_id = self.new_id
        return "; ".join(text_engine.make_duplicate_object(oid, new_id)(run.tree) for oid in ids)


class Rename(_Targeted):
    """Change the id and/or label of one target; references to the old id are rewritten."""

    op: Literal["rename"]
    new_id: str | None = None
    label: str | None = None

    def apply(self, run: _Run) -> str:
        ids = run.targets(self)
        if len(ids) != 1:
            raise run.fail(f"rename needs exactly one target, got {len(ids)}")
        return text_engine.make_rename_object(ids[0], self.new_id, self.label)(run.tree)


class Delete(_Targeted):
    """Delete the targets (HIGH risk: needs an `approval_token`)."""

    RISK: ClassVar[RiskClass] = RiskClass.HIGH

    op: Literal["delete"]

    def apply(self, run: _Run) -> str:
        return text_engine.make_delete_objects(run.targets(self))(run.tree)


# --- transform --------------------------------------------------------------


class Move(_Targeted):
    """Translate each target by `(dx, dy)` (each defaults to 0)."""

    op: Literal["move"]
    dx: float = 0.0
    dy: float = 0.0

    def apply(self, run: _Run) -> str:
        dx, dy = self.dx, self.dy
        return run.each(self, lambda oid: transform_engine.move(run.tree, oid, dx, dy))


class Scale(_Targeted):
    """Scale each target by `sx` (and `sy`, default `sx`) about its own centre."""

    op: Literal["scale"]
    sx: float
    sy: float | None = None

    def apply(self, run: _Run) -> str:
        sx, sy = self.sx, self.sy
        return run.each(self, lambda oid: transform_engine.scale(run.tree, oid, sx, sy))


class Rotate(_Targeted):
    """Rotate each target by `degrees` about its centre (or about `(cx, cy)` when both given)."""

    op: Literal["rotate"]
    degrees: float
    cx: float | None = None
    cy: float | None = None

    def apply(self, run: _Run) -> str:
        deg, cx, cy = self.degrees, self.cx, self.cy
        return run.each(self, lambda oid: transform_engine.rotate(run.tree, oid, deg, cx, cy))


class Tile(_Targeted):
    """Lay out a `rows` x `cols` grid of clones of each target, `dx` / `dy` apart."""

    CREATES: ClassVar[bool] = True

    op: Literal["tile"]
    rows: int
    cols: int
    dx: float
    dy: float

    def apply(self, run: _Run) -> str:
        rows, cols, dx, dy = self.rows, self.cols, self.dx, self.dy
        return run.each(self, lambda oid: transform_engine.tile(run.tree, oid, rows, cols, dx, dy))


class ResizeCanvas(_Op):
    """Set the canvas width/height (optionally adjusting the viewBox, or adding a bleed)."""

    op: Literal["resize_canvas"]
    width: str
    height: str
    adjust_viewbox: bool = False
    bleed: float | None = None
    bleed_color: str = "#ffffff"

    def apply(self, run: _Run) -> str:
        return transform_engine.resize_canvas(
            run.tree,
            self.width,
            self.height,
            self.adjust_viewbox,
            bleed=self.bleed,
            bleed_color=self.bleed_color,
        )


class NormalizeViewbox(_Op):
    """Normalize / repair the root viewBox."""

    op: Literal["normalize_viewbox"]

    def apply(self, run: _Run) -> str:
        return transform_engine.normalize_viewbox(run.tree)


class FitToContent(_Op):
    """Set the root viewBox to the content bounding box (measured by Inkscape)."""

    ENGINE: ClassVar[bool] = True

    op: Literal["fit_to_content"]

    def apply(self, run: _Run) -> str:
        if run.dry_run:
            return "dry-run: would fit the viewBox to the content bounding box"
        return transform_engine.fit_to_content(run.tree, run.ctx.materialize(run.tree))


# --- compose ----------------------------------------------------------------


def _checked_svg(svg: str) -> str:
    try:
        check_input_bytes_size(svg.encode("utf-8"))
    except LimitExceeded as exc:
        raise EditError("input svg exceeds the configured size limit") from exc
    return svg


class SetSvg(_Op):
    """REPLACE the whole document with `svg` (root `<svg>`, allowlist-scrubbed). HIGH risk."""

    RISK: ClassVar[RiskClass] = RiskClass.HIGH
    CAPTURE: ClassVar[bool] = False

    op: Literal["set_svg"]
    svg: str

    def apply(self, run: _Run) -> str:
        return compose_engine.make_set_document_svg(_checked_svg(self.svg))(run.tree)


class InsertSvg(_Op):
    """Graft an SVG fragment under `parent` (default: the root; allowlist-scrubbed). HIGH risk."""

    RISK: ClassVar[RiskClass] = RiskClass.HIGH
    ID_FIELDS: ClassVar[tuple[str, ...]] = ("parent",)
    CREATES: ClassVar[bool] = True

    op: Literal["insert_svg"]
    svg: str
    parent: str | None = None
    unwrap: bool = True

    def apply(self, run: _Run) -> str:
        return compose_engine.make_insert_svg_fragment(
            _checked_svg(self.svg), self.parent, self.unwrap
        )(run.tree)


class Place(_Op):
    """Place another document (or one of its objects) here at `(x, y)` scaled by `scale`.

    A same-document source is read from the CURRENT in-memory state; another document is read from
    its saved working copy. The placed content is wrapped in a new `<g>` (the created id).
    """

    CREATES: ClassVar[bool] = True

    op: Literal["place"]
    source_doc_id: str
    source_id: str | None = None
    x: float
    y: float
    scale: float = 1.0

    def apply(self, run: _Run) -> str:
        if self.source_doc_id == run.ctx.doc_id:
            source_root = run.tree.getroot()
        else:
            _entry, source_root = _load_tree(self.source_doc_id)
        if self.source_id is not None:
            elem = find_by_id(source_root, self.source_id)
            if elem is None:
                raise run.fail(f"source id {self.source_id!r} not found in {self.source_doc_id!r}")
            label = self.source_id
        else:
            elem = source_root
            label = self.source_doc_id
        holder: list[str] = []
        mutate = collection_engine.make_place_document(
            copy.deepcopy(elem), label, self.x, self.y, self.scale, result_holder=holder
        )
        summary = mutate(run.tree)
        run.override = holder[0] if holder else None
        return summary


class Grid(_Op):
    """Lay documents (`doc_ids`) or objects of one document (`source_doc_id` + `source_ids`) out in
    a `rows` x `cols` grid of `cell`-sized cells in THIS document (sources are read from their saved
    working copies)."""

    CREATES: ClassVar[bool] = True

    op: Literal["grid"]
    rows: int
    cols: int
    cell: float
    doc_ids: list[str] | None = None
    source_doc_id: str | None = None
    source_ids: list[str] | None = None
    gap: float = 0.0
    scale_to_fit: bool = True
    padding: float = 0.0

    def apply(self, run: _Run) -> str:
        pairs = collection_engine.resolve_grid_assets(
            doc_ids=self.doc_ids, source_ids=self.source_ids, source_doc_id=self.source_doc_id
        )
        mutate, plan = collection_engine.make_compose_grid(
            pairs,
            self.rows,
            self.cols,
            self.cell,
            gap=self.gap,
            scale_to_fit=self.scale_to_fit,
            padding=self.padding,
        )
        summary = mutate(run.tree)
        run.override = plan[0].group_id if plan else None
        return summary


# --- paths (Inkscape-backed, HIGH, raw_action_enabled only) -----------------


class _PathOp(_Targeted):
    """A path geometry op run by Inkscape on the CURRENT in-memory tree."""

    RISK: ClassVar[RiskClass] = RiskClass.HIGH
    ENGINE: ClassVar[bool] = True
    RAW_ACTION: ClassVar[bool] = True
    CAPTURE: ClassVar[bool] = False
    #: the `edit.paths` op token
    TOKEN: ClassVar[str]

    def apply(self, run: _Run) -> str:
        ids = run.targets(self)
        if run.dry_run:
            summary = paths_engine.describe_dry_run(run.tree.getroot(), self.TOKEN, ids)
            run.override = paths_engine.predicted_result_id(run.tree.getroot(), self.TOKEN, ids)
            return summary
        holder: list[str] = []
        summary = paths_engine.apply_path_op(
            run.tree, run.ctx.materialize(run.tree), self.TOKEN, ids, result_holder=holder
        )
        run.override = holder[0] if holder else None
        return summary


class Simplify(_PathOp):
    """Simplify the targeted paths (fewer nodes; lossy)."""

    TOKEN: ClassVar[str] = paths_engine.SIMPLIFY
    op: Literal["simplify"]


class Union(_PathOp):
    """Boolean union of >= 2 targets into one path (the bottom-most id survives)."""

    TOKEN: ClassVar[str] = paths_engine.UNION
    CREATES: ClassVar[bool] = True
    op: Literal["union"]


class Difference(_PathOp):
    """Boolean difference: the bottom-most target minus the others (its id survives)."""

    TOKEN: ClassVar[str] = paths_engine.DIFFERENCE
    CREATES: ClassVar[bool] = True
    op: Literal["difference"]


class Combine(_PathOp):
    """Combine >= 2 paths into one compound path (the bottom-most id survives)."""

    TOKEN: ClassVar[str] = paths_engine.COMBINE
    CREATES: ClassVar[bool] = True
    op: Literal["combine"]


class BreakApart(_PathOp):
    """Break compound paths into their sub-paths."""

    TOKEN: ClassVar[str] = paths_engine.BREAK_APART
    op: Literal["break_apart"]


class StrokeToPath(_PathOp):
    """Convert strokes to filled outline paths."""

    TOKEN: ClassVar[str] = paths_engine.STROKE_TO_PATH
    op: Literal["stroke_to_path"]


class Cleanup(_PathOp):
    """Clean up the targeted paths (alias of simplify in Inkscape 1.4)."""

    TOKEN: ClassVar[str] = paths_engine.CLEANUP
    op: Literal["cleanup"]


# --- optimize ---------------------------------------------------------------


class OptimizeWeb(_Op):
    """Web-optimize: strip editor data / unused defs, drop empty groups, round numbers.

    Ids, layers and layer locks are kept so the document stays editable; `aggressive=true` (HIGH
    risk, needs approval_token) also drops unreferenced ids, layers and namedview (the last step
    before shipping). Not a sanitizer: scripts and event attributes are left as they are.
    """

    op: Literal["optimize_web"]
    precision: int = optimize_engine.DEFAULT_PRECISION
    keep_ids: list[str] | None = None
    aggressive: bool = False

    def effective_risk(self) -> RiskClass:
        return RiskClass.HIGH if self.aggressive else self.RISK

    def apply(self, run: _Run) -> str:
        return optimize_engine.optimize_web_mutate(
            self.precision, self.keep_ids, aggressive=self.aggressive
        )(run.tree)


#: The discriminated union of every typed op. pydantic selects the concrete model by the `op` tag,
#: so each op is parsed and validated against its own schema.
Op = Annotated[
    CreateRect
    | CreateCircle
    | CreateEllipse
    | CreateLine
    | CreatePolygon
    | CreatePolyline
    | CreatePath
    | CreateText
    | CreateGroup
    | CreateUse
    | LinearGradient
    | RadialGradient
    | Group
    | Reparent
    | Fill
    | Stroke
    | Opacity
    | ReplaceColor
    | Palette
    | Font
    | ReplaceText
    | Duplicate
    | Rename
    | Delete
    | Move
    | Scale
    | Rotate
    | Tile
    | ResizeCanvas
    | NormalizeViewbox
    | FitToContent
    | SetSvg
    | InsertSvg
    | Place
    | Grid
    | Simplify
    | Union
    | Difference
    | Combine
    | BreakApart
    | StrokeToPath
    | Cleanup
    | OptimizeWeb,
    Field(discriminator="op"),
]

_OPS_ADAPTER: TypeAdapter[list[Op]] = TypeAdapter(list[Op])


# --- public API -------------------------------------------------------------


def parse_ops(raw: list[Any]) -> list[Op]:
    """Validate + coerce a raw op list into typed ops, or raise :class:`OpError`.

    Each item is matched to its model by the `op` tag. The error names the first three problems as
    `ops[i].field: message`. Idempotent on already-typed ops.
    """
    if not raw:
        raise OpError("ops must contain at least one op")
    if len(raw) > MAX_OPS:
        raise OpError(f"ops has {len(raw)} entries; at most {MAX_OPS} per call, split the call")
    try:
        return _OPS_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        detail = format_validation_errors(exc.errors(include_url=False), root="ops")
        raise OpError(f"invalid ops: {detail}") from exc


def ops_risk(ops: list[Op]) -> RiskClass:
    """Effective risk of a call = the MAX over its ops, never below MEDIUM (every edit writes)."""
    risk = RiskClass.MEDIUM
    for op in ops:
        if _RISK_ORDER[op.effective_risk()] > _RISK_ORDER[risk]:
            risk = op.effective_risk()
    return risk


def _ref_indices(op: _Op) -> list[int]:
    found: list[int] = []
    for name in op.ID_FIELDS:
        value = getattr(op, name)
        for item in [value] if isinstance(value, str) else (value or []):
            m = _REF_RE.match(item)
            if m:
                found.append(int(m.group(1)))
    for name in op.PAINT_FIELDS:
        value = getattr(op, name)
        if isinstance(value, str):
            found.extend(int(m.group(1)) for m in _URL_REF_RE.finditer(value))
    return found


def check_ops(
    ops: list[Op], *, approval_token: str | None = None, dry_run: bool = False
) -> RiskClass:
    """Cheap static pre-flight, before any record is opened; returns the call's risk.

    Rejects `$n` references that are not to an EARLIER id-creating op, raw-action ops while
    `raw_action_enabled` is off, and (unless `dry_run`) a HIGH-risk call without `approval_token`.
    """
    raw_enabled = get_settings().raw_action_enabled
    for i, op in enumerate(ops):
        for k in _ref_indices(op):
            if k >= i:
                raise OpError(
                    f"ops[{i}] {op.op}: ${k} must reference an EARLIER op (this is op {i})",
                    op_index=i,
                )
            if not ops[k].CREATES:
                raise OpError(
                    f"ops[{i}] {op.op}: ${k} refers to op {k} ({ops[k].op}) which creates no id",
                    op_index=i,
                )
        if op.RAW_ACTION and not raw_enabled:
            raise OpError(
                f"ops[{i}] {op.op}: path ops are disabled; the operator must set "
                "INKSCAPE_MCP_RAW_ACTION_ENABLED=1",
                op_index=i,
            )
    risk = ops_risk(ops)
    if risk == RiskClass.HIGH and not dry_run and not (approval_token and approval_token.strip()):
        first = next(i for i, op in enumerate(ops) if op.effective_risk() == RiskClass.HIGH)
        raise OpError(
            f"ops[{first}] {ops[first].op} is HIGH risk; re-call with approval_token",
            op_index=first,
        )
    return risk


def _render_artifact(
    doc_id: str, result: EditResult, registry: Registry | None, warnings: list[str]
) -> str | None:
    """One best-effort render of the (edited) working copy; the workspace-root-relative PNG path.

    Reuses the `edit_previews` after-frame when the pipeline already rendered it.
    """
    reg = registry if registry is not None else get_registry()
    try:
        if result.changed and result.preview_after:
            entry = reg.get(doc_id)
            return (
                (Path(entry.workspace_dir) / result.preview_after)
                .relative_to(Path(entry.root))
                .as_posix()
            )
        return render_preview(doc_id).artifact_path
    except _RENDER_FAILURES as exc:
        _logger.warning(
            "inline render unavailable",
            extra={"event": "render", "doc_id": doc_id, "error": type(exc).__name__},
        )
        warnings.append("render unavailable: Inkscape could not render the document")
        return None


def run_ops(
    doc_id: str,
    ops: list[Any],
    *,
    dry_run: bool = False,
    render: bool = False,
    approval_token: str | None = None,
    registry: Registry | None = None,
    tool: str = "edit",
) -> OpsResult:
    """Parse, check and run an op list as ONE atomic, recorded, reversible edit.

    Raises :class:`OpError` for an invalid list or a failing op (nothing is written; the Operation
    Record is discarded), `EditApplyError` for an unknown document, and `PolicyViolation` if the
    risk policy refuses the call. With `dry_run=True` nothing is recorded or written and the result
    carries the plan. With `render=True` the edited document is rendered once (best effort) and
    `render_artifact` names the PNG.
    """
    typed = parse_ops(ops)
    risk = check_ops(typed, approval_token=approval_token, dry_run=dry_run)
    ctx = EditContext(doc_id, registry)
    run = _Run(typed, ctx, dry_run=dry_run)
    needs_approval = risk == RiskClass.HIGH

    if dry_run:
        try:
            _entry, tree = load_working_tree(doc_id, registry)
        except (KeyError, DocumentNotFound) as exc:
            raise EditApplyError("document id not found") from exc
        try:
            summary = run.execute(copy.deepcopy(tree))
        finally:
            ctx.cleanup()
        return OpsResult(
            doc_id=doc_id,
            changed=False,
            dry_run=True,
            risk=risk.value,
            needs_approval=needs_approval,
            summary=f"dry-run: {summary}",
            created=run.created,
            outcomes=run.outcomes,
            warnings=run.warnings,
        )

    try:
        result = apply_edit(
            doc_id,
            tool,
            {"op_count": len(typed), "ops": [op.op for op in typed]},
            run.execute,
            registry=registry,
            approval_token=approval_token,
            risk_class=risk,
        )
    finally:
        ctx.cleanup()

    render_artifact = _render_artifact(doc_id, result, registry, run.warnings) if render else None
    return OpsResult(
        doc_id=doc_id,
        changed=result.changed,
        operation_id=result.operation_id,
        snapshot_id=result.snapshot_id,
        risk=risk.value,
        needs_approval=needs_approval,
        summary=result.summary or "",
        created=run.created,
        outcomes=run.outcomes,
        warnings=run.warnings,
        render_artifact=render_artifact,
    )


__all__ = [
    "DEFAULT_MAX_MATCHES",
    "MAX_OPS",
    "CreatedRef",
    "EditContext",
    "Op",
    "OpError",
    "OpOutcome",
    "OpsResult",
    "Selector",
    "Stop",
    "check_ops",
    "ops_risk",
    "parse_ops",
    "run_ops",
]
