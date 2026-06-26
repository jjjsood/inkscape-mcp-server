"""Constrained computed-edit path (E20-02): ``transform_objects``.

A declarative SELECTOR → ONE TYPED OPERATION spec — Penpot-style bulk editing ("recolour every blue
rect", "nudge every text") in ONE call, but WITHOUT a code escape hatch. It resolves a target SET
via the EXISTING `find_objects` predicate engine and applies ONE typed op (from the `apply_edits`
member set) to EVERY matched object, atomically and reversibly.

There is no second matcher and no second kernel — it is pure composition (ADR-002/003):

* the SELECTOR reuses `inkscape_mcp.document.inspect.find_objects` VERBATIM (tag / fill / stroke /
  text / id_prefix / bbox, full CSS-cascade paint resolution) — the same engine the `find_objects`
  tool exposes;
* the OPERATION is ONE member of a discriminated union (`TargetedOp`) over the typed-DOM ops, but
  WITHOUT its target ids — the matched ids are FANNED OUT into it. Each `TargetedOp` member expands
  to one or more `apply_edits` `TypedEdit` dicts (an op that takes `object_ids` becomes ONE edit
  covering the whole matched list; an op that takes a single `object_id` becomes N edits, one per
  match), which are then fed through the SAME `build_batch` → `apply_edit` path E19-01 built;
* so the whole transform lands as ONE snapshot + ONE Operation Record (the batch kernel does this
  per `apply_edit` call) and a single `restore_snapshot` reverts it; any per-edit failure rolls the
  WHOLE thing back atomically (validate-all-first, all-or-nothing — from the batch kernel);
* the effective risk is the op's class: a HIGH op (`delete_object`) escalates the operation to HIGH
  and forces the same per-op `approval_token` gate that member would demand alone (`batch_risk`).

ACCEPTED OP SET (the constraint decision): only ops that apply the SAME change to an EXISTING object
targeted by id, where applying identical arguments to N matches is MEANINGFUL — `set_fill` /
`set_stroke` / `set_opacity` / `set_font` / `move_object` / `scale_object` / `rotate_object` /
`delete_object`. Deliberately REJECTED (with a clear message):

* document- / scope-wide ops with no per-match target — `replace_color`, `apply_palette`,
  `resize_canvas`, `normalize_viewbox` (not meaningful "per match");
* element-CREATION ops — every `create_*`, `group_objects`, `create_use`, `add_*_gradient`,
  `tile`, `reparent_object` (they ADD/relocate structure, they do not transform the matched set);
* identity-conflicting per-id ops — `rename_object`, `replace_text`, `duplicate_object` (applying
  ONE identical id / text / copy to N matches collides or is degenerate).

`dry_run` defaults to True (mirrors `export_batch`): the call resolves + validates the plan and
returns the matched id set + the projected per-edit ops, mutating NOTHING. `dry_run=False` performs
the mutation. A bounded `max_matches` cap REJECTS an over-broad selector BEFORE any mutation
(sec.12).

SECURITY (sec.12): no shell strings, no raw Action, no free text. The selector flows through the
read-only `find_objects` engine; matched ids are used only as in-tree lookup keys handed to typed
batch validators (never argv); every value still passes the same `edit.dom` / `edit.style`
normalizers the single-edit tools use. The op set is bounded and the match count is capped. Errors
map to `ToolError` with stable, host-path-free messages.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Literal

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from inkscape_mcp.document.inspect import (
    BBox,
    DocumentNotFound,
    InspectionError,
)
from inkscape_mcp.document.inspect import (
    find_objects as _find_objects,
)
from inkscape_mcp.edit.batch import (
    BatchTooLarge,
    InvalidBatchEdit,
    batch_risk,
    build_batch,
    coerce_edits,
)
from inkscape_mcp.edit.dom import EditError, TargetNotFound
from inkscape_mcp.edit.pipeline import EditApplyError, apply_edit
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.server import mcp
from inkscape_mcp.workspace.risk import PolicyViolation

_logger = get_logger("tools.transform_objects")

#: Default cap on the matched set. An over-broad selector that resolves more than this REJECTS the
#: call BEFORE any mutation (sec.12) — the caller can lower it, but never silently fan a transform
#: across an unbounded number of objects. Aligned with the batch surface's blast-radius bound.
DEFAULT_MAX_MATCHES = 64


class TransformSelector(BaseModel):
    """The target selector — the SAME predicate fields `find_objects` takes (no new logic).

    Every supplied filter is ANDed; an unset filter is ignored. With no filters at all the selector
    matches every addressable object (then bounded by `max_matches`). `tag` is an exact local
    element name; `fill` / `stroke` match the EFFECTIVE cascade-resolved paint (casing- / shorthand-
    insensitive); `text` is a case-insensitive substring; `id_prefix` an id prefix; `bbox` an
    intersection box. `accurate_bbox` opts into geometry-accurate boxes (one read-only Inkscape
    `--query-all`) so transformed / path / text objects can match a `bbox`.
    """

    tag: str | None = None
    fill: str | None = None
    stroke: str | None = None
    text: str | None = None
    id_prefix: str | None = None
    bbox: BBox | None = None
    accurate_bbox: bool = False


# --- the targeted-op union: each member = an `apply_edits` op WITHOUT its target ids ----------
#
# Each member carries the op's typed PARAMS only; the matched ids are injected by `expand`. `expand`
# returns a list of raw `apply_edits` `TypedEdit` dicts — reusing those exact models means the op
# params validate IDENTICALLY to `apply_edits` (single-sourced; no second schema). An op that takes
# `object_ids` (a list) expands to ONE edit covering the whole matched set; an op that takes one
# `object_id` expands to N edits, one per matched id.


class _TargetedOp(BaseModel):
    """Base for one targeted op: a typed op spec minus its target ids, plus `expand`.

    `RISK` mirrors the matching `apply_edits` member's class so the operation's effective risk is
    the op's class (escalated via `batch_risk` over the expanded edits).
    """

    RISK: ClassVar[str] = "medium"

    def expand(self, ids: list[str]) -> list[dict[str, Any]]:  # pragma: no cover - overridden
        raise NotImplementedError


class _ListTargetOp(_TargetedOp):
    """An op whose `apply_edits` member targets a LIST (`object_ids`) — ONE edit, all matches."""

    def _payload(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def expand(self, ids: list[str]) -> list[dict[str, Any]]:
        return [{**self._payload(), "object_ids": ids}]


class _SingleTargetOp(_TargetedOp):
    """An op whose `apply_edits` member targets ONE `object_id` — N edits, one per matched id."""

    def _payload(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def expand(self, ids: list[str]) -> list[dict[str, Any]]:
        return [{**self._payload(), "object_id": oid} for oid in ids]


# --- style ops (target a list) -----------------------------------------------


class SetFillOp(_ListTargetOp):
    """Set fill colour (+ optional fill opacity) on every match — mirrors ``set_fill``."""

    op: Literal["set_fill"]
    color: str
    opacity: float | None = None

    def _payload(self) -> dict[str, Any]:
        return {"op": "set_fill", "color": self.color, "opacity": self.opacity}


class SetStrokeOp(_ListTargetOp):
    """Set stroke colour / width / opacity on every match — mirrors ``set_stroke``."""

    op: Literal["set_stroke"]
    color: str | None = None
    width: str | None = None
    opacity: float | None = None

    def _payload(self) -> dict[str, Any]:
        return {
            "op": "set_stroke",
            "color": self.color,
            "width": self.width,
            "opacity": self.opacity,
        }


class SetOpacityOp(_ListTargetOp):
    """Set element-level opacity on every match — mirrors ``set_opacity``."""

    op: Literal["set_opacity"]
    opacity: float

    def _payload(self) -> dict[str, Any]:
        return {"op": "set_opacity", "opacity": self.opacity}


class SetFontOp(_ListTargetOp):
    """Set font family / size / weight on every match — mirrors ``set_font``."""

    op: Literal["set_font"]
    family: str | None = None
    size: str | None = None
    weight: str | None = None

    def _payload(self) -> dict[str, Any]:
        return {"op": "set_font", "family": self.family, "size": self.size, "weight": self.weight}


class DeleteOp(_ListTargetOp):
    """Delete every match — mirrors ``delete_object``. HIGH risk (forces the approval gate)."""

    RISK: ClassVar[str] = "high"

    op: Literal["delete_object"]

    def _payload(self) -> dict[str, Any]:
        return {"op": "delete_object"}


# --- simple transforms (target one id each) ----------------------------------


class MoveOp(_SingleTargetOp):
    """Translate every match by (dx, dy) — mirrors ``move_object``."""

    op: Literal["move_object"]
    dx: float
    dy: float

    def _payload(self) -> dict[str, Any]:
        return {"op": "move_object", "dx": self.dx, "dy": self.dy}


class ScaleOp(_SingleTargetOp):
    """Scale every match by (sx, sy) — mirrors ``scale_object``."""

    op: Literal["scale_object"]
    sx: float
    sy: float | None = None

    def _payload(self) -> dict[str, Any]:
        return {"op": "scale_object", "sx": self.sx, "sy": self.sy}


class RotateOp(_SingleTargetOp):
    """Rotate every match by `degrees` (optional pivot) — mirrors ``rotate_object``."""

    op: Literal["rotate_object"]
    degrees: float
    cx: float | None = None
    cy: float | None = None

    def _payload(self) -> dict[str, Any]:
        return {"op": "rotate_object", "degrees": self.degrees, "cx": self.cx, "cy": self.cy}


#: The accepted targeted-op union. pydantic/FastMCP select the concrete model by the ``op`` tag, so
#: the op params validate against the SAME schema the matching `apply_edits` member uses — this is
#: the constrained op set: identical-argument-per-match style / transform / delete ops only (the
#: document-wide, create, and identity-conflicting ops are deliberately excluded — see module docs).
TargetedOp = Annotated[
    SetFillOp | SetStrokeOp | SetOpacityOp | SetFontOp | DeleteOp | MoveOp | ScaleOp | RotateOp,
    Field(discriminator="op"),
]


#: Validate + coerce the selector / operation from a raw dict (a DIRECT Python call and the test
#: suite pass plain dicts; FastMCP already coerces over the wire). An unknown `op` or a bad field
#: raises `ValidationError`, mapped to a stable `ToolError` — so a rejected op outside the accepted
#: set fails cleanly, and both call paths validate identically.
_SELECTOR_ADAPTER: TypeAdapter[TransformSelector] = TypeAdapter(TransformSelector)
_OPERATION_ADAPTER: TypeAdapter[TargetedOp] = TypeAdapter(TargetedOp)


class TransformPlanEdit(BaseModel):
    """One projected `apply_edits` edit in the plan (the op + the id(s) it would target)."""

    op: str = Field(description="The typed `op` discriminator of this projected edit.")
    object_ids: list[str] = Field(
        description="The matched id(s) this edit would target (one per single-target edit)."
    )


class TransformObjectsResult(BaseModel):
    """Result of `transform_objects`: the matched set + the projected/applied plan.

    `matched_ids` is the resolved target set (selector → `find_objects`). `match_count` is its size.
    `risk_class` is the operation's effective risk (the op's class). `dry_run` echoes the mode.
    `plan` is the ordered list of projected `apply_edits` edits (op + target id(s)) — present on
    a dry run and a real run, so the caller can see exactly what was / would be applied.

    On a DRY RUN nothing is mutated: `applied` is False, `operation_id` / `snapshot_id` are empty.
    On a REAL RUN (`dry_run=False`) `applied` reflects whether a real content change landed (a
    genuine no-op leaves them empty, exactly as for a single edit); `operation_id` / `snapshot_id`
    identify the ONE Operation Record + its pre-mutation snapshot (revert the whole transform with
    `restore_snapshot`), and `changed` / `summary` come from the batch kernel.
    """

    doc_id: str
    matched_ids: list[str]
    match_count: int
    risk_class: str
    dry_run: bool
    plan: list[TransformPlanEdit]
    applied: bool = False
    changed: bool = False
    operation_id: str = ""
    snapshot_id: str = ""
    summary: str | None = None


@mcp.tool
def transform_objects(
    doc_id: str,
    selector: TransformSelector,
    operation: TargetedOp,
    dry_run: bool = True,
    max_matches: int = DEFAULT_MAX_MATCHES,
    approval_token: str | None = None,
) -> TransformObjectsResult:
    """Apply ONE typed op to EVERY object a selector matches — one atomic, reversible operation.

    When to use: bulk editing keyed by a predicate rather than by hand-listing ids — "recolour every
    blue rect", "nudge every text down 4px", "delete every object whose id starts with tmp-". It is
    `find_objects` (the SELECTOR) wired to ONE typed op fanned across the matches, run through the
    SAME atomic batch kernel as `apply_edits`. For a known id list call the dedicated tool
    (`set_fill`, `move_object`, …) or `apply_edits` directly; this adds NO authority — only the
    select-then-apply fan-out (ADR-002/003: no free text, no raw Action, no loops/expressions).

    Key params: `selector` is the SAME predicate `find_objects` takes (`tag` / `fill` / `stroke` /
    `text` / `id_prefix` / `bbox`, full CSS-cascade paint match); `operation` is exactly ONE op
    tagged by an `op` field — the accepted set is `set_fill` / `set_stroke` / `set_opacity` /
    `set_font` / `move_object` / `scale_object` / `rotate_object` / `delete_object` (high), each
    with the SAME params as its dedicated tool MINUS the target ids (those from the selector), e.g.
    `{"op": "set_fill", "color": "#3366cc"}`, `{"op": "move_object", "dx": 0, "dy": 4}`. Document-
    wide ops (`replace_color`, `apply_palette`, `resize_canvas`, `normalize_viewbox`), element
    CREATION ops, and identity-conflicting per-id ops (`rename_object`, `replace_text`,
    `duplicate_object`) are NOT accepted — they are not meaningful applied identically per match.
    `dry_run=True` (DEFAULT) resolves + validates and returns the matched ids + the projected plan,
    writing NOTHING; `dry_run=False` performs it. `max_matches` (default 64) REJECTS an over-broad
    selector before any mutation. A `delete_object` op makes the operation HIGH and requires a
    non-empty `approval_token`.

    Render and look before you trust it: a transform changes many objects at once — call
    `render_preview` (or `live_render_view` in live mode) afterwards and inspect the result, and
    `restore_snapshot(doc_id, snapshot_id)` reverts the WHOLE transform in one step.

    Return shape: `TransformObjectsResult` — `matched_ids` + `match_count`, the effective
    `risk_class`, the `dry_run` flag, the projected `plan` (per-edit op + target id(s)); on a real
    run also `applied` / `changed` / `summary` and the single `operation_id` / `snapshot_id` (the
    revert target).

    Example: `transform_objects(doc_id, {"tag": "rect", "fill": "#3366cc"}, {"op": "set_fill",
    "color": "#ff0000"}, dry_run=False)`

    Risk class: medium (the effective risk is the op's class; a `delete_object` op escalates the
    operation to high and requires `approval_token`). Reversible via the pre-transform snapshot.
    """
    if max_matches < 1:
        raise ToolError("max_matches must be at least 1")

    # Coerce + validate selector / operation identically for the wire AND a direct Python call. An
    # unknown `op` (an op outside the accepted set) or a bad field raises here, before any work.
    try:
        selector = _SELECTOR_ADAPTER.validate_python(selector)
        operation = _OPERATION_ADAPTER.validate_python(operation)
    except ValidationError as exc:
        raise ToolError(
            f"invalid selector or operation: {exc.error_count()} validation error(s)"
        ) from exc

    # 1. SELECTOR — resolve the target set through the read-only find_objects engine (verbatim).
    try:
        found = _find_objects(
            doc_id,
            tag=selector.tag,
            fill=selector.fill,
            stroke=selector.stroke,
            text=selector.text,
            id_prefix=selector.id_prefix,
            bbox=selector.bbox,
            accurate_bbox=selector.accurate_bbox,
        )
    except DocumentNotFound as exc:
        raise ToolError("document id not found") from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc

    matched_ids = [obj.object_id for obj in found.objects]

    # 2. BOUND — reject an over-broad selector BEFORE building or applying any edit (sec.12).
    if found.count > max_matches:
        raise ToolError(
            f"selector matched {found.count} objects, exceeding max_matches={max_matches}; "
            "narrow the selector or raise max_matches"
        )

    # 3. EXPAND — fan the one typed op across the matched ids into raw apply_edits edits, then
    #    coerce them through the SAME batch validators (single-sourced schema; no second matcher).
    raw_edits = operation.expand(matched_ids) if matched_ids else []
    risk = batch_risk(coerce_edits(raw_edits)) if raw_edits else _risk_for(operation)
    plan = [TransformPlanEdit(op=edit["op"], object_ids=_plan_ids(edit)) for edit in raw_edits]

    # 4a. DRY RUN — return the matched set + projected plan; mutate nothing.
    if dry_run:
        log_tool_call(
            _logger,
            tool="transform_objects",
            doc_id=doc_id,
            dry_run=True,
            matched=found.count,
            op=plan[0].op if plan else "(none)",
        )
        return TransformObjectsResult(
            doc_id=doc_id,
            matched_ids=matched_ids,
            match_count=found.count,
            risk_class=risk.value,
            dry_run=True,
            plan=plan,
        )

    # 4b. A selector that matched nothing is a no-op on a real run too — nothing to apply.
    if not raw_edits:
        return TransformObjectsResult(
            doc_id=doc_id,
            matched_ids=[],
            match_count=0,
            risk_class=risk.value,
            dry_run=False,
            plan=[],
            applied=False,
            changed=False,
            summary="no change: selector matched no objects",
        )

    # 5. APPLY — feed the expanded edits through the SAME build_batch → apply_edit path as
    #    apply_edits: validate-all-first, atomic, ONE snapshot + ONE Operation Record, risk-gated.
    try:
        mutate, batch_risk_class, op_names = build_batch(raw_edits)
        result = apply_edit(
            doc_id,
            "transform_objects",
            {"edit_count": len(op_names), "ops": op_names, "match_count": found.count},
            mutate,
            approval_token=approval_token,
            risk_class=batch_risk_class,
        )
    except (BatchTooLarge, InvalidBatchEdit) as exc:
        raise ToolError(str(exc)) from exc
    except (EditApplyError, DocumentNotFound, KeyError) as exc:
        raise ToolError("document id not found") from exc
    except TargetNotFound as exc:
        raise ToolError("object id not found in document") from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc
    except PolicyViolation as exc:
        raise ToolError(str(exc)) from exc
    except EditError as exc:
        raise ToolError(str(exc)) from exc

    log_tool_call(
        _logger,
        tool="transform_objects",
        doc_id=doc_id,
        dry_run=False,
        matched=found.count,
        operation_id=result.operation_id,
    )
    return TransformObjectsResult(
        doc_id=doc_id,
        matched_ids=matched_ids,
        match_count=found.count,
        risk_class=batch_risk_class.value,
        dry_run=False,
        plan=plan,
        applied=result.changed,
        changed=result.changed,
        operation_id=result.operation_id,
        snapshot_id=result.snapshot_id,
        summary=result.summary,
    )


def _plan_ids(edit: dict[str, Any]) -> list[str]:
    """The id(s) a projected edit targets — `object_ids` (list) or a single `object_id`."""
    if "object_ids" in edit:
        ids = edit["object_ids"]
        return list(ids) if isinstance(ids, list) else [str(ids)]
    return [str(edit["object_id"])] if "object_id" in edit else []


def _risk_for(operation: TargetedOp) -> Any:
    """Effective risk when the selector matched nothing (no edits to derive it from).

    Maps the op's declared `RISK` string to the canonical `RiskClass` so an empty-match dry run
    reports the operation's risk tier. Imported lazily to mirror the batch kernel's vocabulary.
    """
    from inkscape_mcp.workspace.risk import RiskClass

    return RiskClass(getattr(operation, "RISK", "medium"))
