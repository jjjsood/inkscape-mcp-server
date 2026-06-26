"""Typed DOM-edit BATCH engine (E19-01).

One typed, validated, ATOMIC batch of N direct-DOM edits applied through the SINGLE existing edit
kernel (:func:`inkscape_mcp.edit.pipeline.apply_edit`). This closes the round-trip tax the
small-typed-tool model pays versus a single free-text ``execute_code`` call (the Penpot survey's one
real finding) WITHOUT giving up typing, validation, or reversibility:

* a batch is an ORDERED LIST OF TYPED EDITS (:data:`TypedEdit`), a discriminated union over the
  existing typed DOM ops — never free text, never a raw-action path (ADR-002/003);
* it reuses the per-op engine builders verbatim (``set_fill_mutate``, ``make_create_rect``,
  ``engine.move`` …), so the single-edit and batch paths can never diverge — the kernel stays
  single-sourced;
* it is two-phase, mirroring ``validate_action_chain`` → ``run_action_chain``:

  - **validate-all** first: :func:`build_batch` constructs EVERY member's ``mutate`` closure up
    front, so schema + value validation (a bad colour, a non-finite number, a malformed id) raises
    BEFORE :func:`apply_edit` opens an Operation Record — nothing is written for a malformed batch;
  - **apply**: the returned ``mutate`` runs the members IN ORDER against ONE in-memory tree. Because
    the kernel only writes the working copy AFTER ``mutate`` returns, any member raising
    ``EditError`` / ``TargetNotFound`` aborts the whole batch before a single byte lands on disk —
    all-or-nothing rollback for free (the document stays byte-identical);

* the whole batch lands under ONE snapshot + ONE Operation Record (the kernel does this per
  ``apply_edit`` call), so a single ``restore_snapshot`` reverts the entire batch (ADR-004);
* the batch's effective risk is the MAX over its members (:func:`batch_risk`): a ``high`` member
  (e.g. ``delete``) escalates the whole batch to ``high`` and forces the same per-op
  ``approval_token`` gate that member would demand alone.

Scope: the union covers the pure direct-DOM ops (ADR-005) — style, text/object, simple transforms,
and element creation. Ops that need the Inkscape engine or cross-document source resolution
(``fit_to_content``, the ``paths`` geometry tools, ``compose_grid`` / ``place_document``, Action
chains) are deliberately OUT of the batch surface; call those tools individually.

SECURITY (sec.12): every value still flows through the same ``edit.dom`` / ``edit.style`` /
``edit.create`` validators the single-edit tools use — object ids are used only for in-tree lookup
(never argv), and no value reaches an attribute without its normalizer. The batch adds no new
authority: it is N existing typed edits composed under one record, nothing more.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Literal

from lxml import etree
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from inkscape_mcp.edit import transform as transform_engine
from inkscape_mcp.edit.create import (
    make_add_linear_gradient,
    make_add_radial_gradient,
    make_create_circle,
    make_create_ellipse,
    make_create_group,
    make_create_line,
    make_create_path,
    make_create_polygon,
    make_create_polyline,
    make_create_rect,
    make_create_text,
    make_create_use,
    make_group_objects,
    make_reparent_object,
)
from inkscape_mcp.edit.pipeline import MutateFn
from inkscape_mcp.edit.style import (
    apply_palette_mutate,
    replace_color_mutate,
    set_fill_mutate,
    set_opacity_mutate,
    set_stroke_mutate,
)
from inkscape_mcp.edit.text_object import (
    make_delete_objects,
    make_duplicate_object,
    make_rename_object,
    make_replace_text,
    make_set_font,
)
from inkscape_mcp.workspace.risk import RiskClass

#: Hard cap on the number of edits in one batch. A batch is a deliberate composition, not an
#: unbounded program: this bounds the in-memory work, the Operation Record size, and the blast
#: radius of one approval. Well above any realistic hand-authored batch.
MAX_BATCH_EDITS = 64

#: Total ordering over the risk vocabulary so a batch's effective risk is the MAX over its members.
_RISK_ORDER: dict[RiskClass, int] = {
    RiskClass.LOW: 0,
    RiskClass.MEDIUM: 1,
    RiskClass.HIGH: 2,
    RiskClass.RESTRICTED: 3,
}


class _Edit(BaseModel):
    """Base for one typed edit in a batch.

    Each concrete subclass carries a `Literal` ``op`` tag (the discriminator), the op's typed
    parameters, a class-level :data:`RISK`, and a :meth:`build` that returns the EXISTING engine
    ``mutate`` closure for that op — so the batch is literally N single-edit kernels run in order.
    """

    #: Effective risk of this single member (escalated to the batch max by :func:`batch_risk`).
    RISK: ClassVar[RiskClass] = RiskClass.MEDIUM

    def build(self) -> MutateFn:  # pragma: no cover - overridden by every concrete subclass
        raise NotImplementedError


# --- style (E2-01) ----------------------------------------------------------


class SetFillEdit(_Edit):
    """Set fill colour (+ optional fill opacity) — mirrors ``set_fill``."""

    op: Literal["set_fill"]
    object_ids: list[str]
    color: str
    opacity: float | None = None

    def build(self) -> MutateFn:
        return set_fill_mutate(self.object_ids, self.color, self.opacity)


class SetStrokeEdit(_Edit):
    """Set stroke colour / width / opacity — mirrors ``set_stroke``."""

    op: Literal["set_stroke"]
    object_ids: list[str]
    color: str | None = None
    width: str | None = None
    opacity: float | None = None

    def build(self) -> MutateFn:
        return set_stroke_mutate(self.object_ids, self.color, self.width, self.opacity)


class SetOpacityEdit(_Edit):
    """Set element-level opacity — mirrors ``set_opacity``."""

    op: Literal["set_opacity"]
    object_ids: list[str]
    opacity: float

    def build(self) -> MutateFn:
        return set_opacity_mutate(self.object_ids, self.opacity)


class ReplaceColorEdit(_Edit):
    """Replace one colour document-wide (or within a scope) — mirrors ``replace_color``."""

    op: Literal["replace_color"]
    from_color: str
    to_color: str
    scope_ids: list[str] | None = None

    def build(self) -> MutateFn:
        return replace_color_mutate(self.from_color, self.to_color, self.scope_ids)


class ApplyPaletteEdit(_Edit):
    """Apply many colour replacements — mirrors ``apply_palette``."""

    op: Literal["apply_palette"]
    mapping: dict[str, str]
    scope_ids: list[str] | None = None

    def build(self) -> MutateFn:
        return apply_palette_mutate(self.mapping, self.scope_ids)


# --- text / object (E2-02 / E16-08) -----------------------------------------


class ReplaceTextEdit(_Edit):
    """Replace a text element's content — mirrors ``replace_text``."""

    op: Literal["replace_text"]
    object_id: str
    text: str

    def build(self) -> MutateFn:
        return make_replace_text(self.object_id, self.text)


class SetFontEdit(_Edit):
    """Set font family / size / weight — mirrors ``set_font``."""

    op: Literal["set_font"]
    object_ids: list[str]
    family: str | None = None
    size: str | None = None
    weight: str | None = None

    def build(self) -> MutateFn:
        return make_set_font(self.object_ids, self.family, self.size, self.weight)


class DuplicateObjectEdit(_Edit):
    """Duplicate an object/group in place — mirrors ``duplicate_object``."""

    op: Literal["duplicate_object"]
    object_id: str
    new_id: str | None = None

    def build(self) -> MutateFn:
        return make_duplicate_object(self.object_id, self.new_id)


class RenameObjectEdit(_Edit):
    """Change an object's id and/or label — mirrors ``rename_object``."""

    op: Literal["rename_object"]
    object_id: str
    new_id: str | None = None
    label: str | None = None

    def build(self) -> MutateFn:
        return make_rename_object(self.object_id, self.new_id, self.label)


class DeleteObjectEdit(_Edit):
    """Delete objects by id — mirrors ``delete_object``. HIGH risk (escalates the batch)."""

    RISK: ClassVar[RiskClass] = RiskClass.HIGH

    op: Literal["delete_object"]
    object_ids: list[str]

    def build(self) -> MutateFn:
        return make_delete_objects(self.object_ids)


# --- simple transforms (E2-03) ----------------------------------------------


class MoveObjectEdit(_Edit):
    """Translate an object — mirrors ``move_object``."""

    op: Literal["move_object"]
    object_id: str
    dx: float
    dy: float

    def build(self) -> MutateFn:
        object_id, dx, dy = self.object_id, self.dx, self.dy

        def mutate(tree: etree._ElementTree) -> str:
            return transform_engine.move(tree, object_id, dx, dy)

        return mutate


class ScaleObjectEdit(_Edit):
    """Scale an object — mirrors ``scale_object``."""

    op: Literal["scale_object"]
    object_id: str
    sx: float
    sy: float | None = None

    def build(self) -> MutateFn:
        object_id, sx, sy = self.object_id, self.sx, self.sy

        def mutate(tree: etree._ElementTree) -> str:
            return transform_engine.scale(tree, object_id, sx, sy)

        return mutate


class RotateObjectEdit(_Edit):
    """Rotate an object — mirrors ``rotate_object``."""

    op: Literal["rotate_object"]
    object_id: str
    degrees: float
    cx: float | None = None
    cy: float | None = None

    def build(self) -> MutateFn:
        object_id, degrees, cx, cy = self.object_id, self.degrees, self.cx, self.cy

        def mutate(tree: etree._ElementTree) -> str:
            return transform_engine.rotate(tree, object_id, degrees, cx, cy)

        return mutate


class ResizeCanvasEdit(_Edit):
    """Set the canvas width/height — mirrors ``resize_canvas``."""

    op: Literal["resize_canvas"]
    width: str
    height: str
    adjust_viewbox: bool = False
    bleed: float | None = None
    bleed_color: str = "#ffffff"

    def build(self) -> MutateFn:
        width, height = self.width, self.height
        adjust_viewbox, bleed, bleed_color = self.adjust_viewbox, self.bleed, self.bleed_color

        def mutate(tree: etree._ElementTree) -> str:
            return transform_engine.resize_canvas(
                tree, width, height, adjust_viewbox, bleed=bleed, bleed_color=bleed_color
            )

        return mutate


class NormalizeViewboxEdit(_Edit):
    """Normalize/repair the root viewBox — mirrors ``normalize_viewbox``."""

    op: Literal["normalize_viewbox"]

    def build(self) -> MutateFn:
        def mutate(tree: etree._ElementTree) -> str:
            return transform_engine.normalize_viewbox(tree)

        return mutate


class TileEdit(_Edit):
    """Lay out a grid of an object — mirrors ``tile``."""

    op: Literal["tile"]
    object_id: str
    rows: int
    cols: int
    dx: float
    dy: float

    def build(self) -> MutateFn:
        object_id, rows, cols = self.object_id, self.rows, self.cols
        dx, dy = self.dx, self.dy

        def mutate(tree: etree._ElementTree) -> str:
            return transform_engine.tile(tree, object_id, rows, cols, dx, dy)

        return mutate


# --- element creation (E14-01 / E14-04) -------------------------------------


class CreateRectEdit(_Edit):
    """Create a ``<rect>`` — mirrors ``create_rect``."""

    op: Literal["create_rect"]
    x: float
    y: float
    width: float
    height: float
    parent_id: str | None = None
    object_id: str | None = None
    rx: float | None = None
    ry: float | None = None
    fill: str | None = None
    stroke: str | None = None
    stroke_width: str | None = None

    def build(self) -> MutateFn:
        return make_create_rect(
            self.x,
            self.y,
            self.width,
            self.height,
            parent_id=self.parent_id,
            object_id=self.object_id,
            rx=self.rx,
            ry=self.ry,
            fill=self.fill,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
        )


class CreateCircleEdit(_Edit):
    """Create a ``<circle>`` — mirrors ``create_circle``."""

    op: Literal["create_circle"]
    cx: float
    cy: float
    r: float
    parent_id: str | None = None
    object_id: str | None = None
    fill: str | None = None
    stroke: str | None = None
    stroke_width: str | None = None

    def build(self) -> MutateFn:
        return make_create_circle(
            self.cx,
            self.cy,
            self.r,
            parent_id=self.parent_id,
            object_id=self.object_id,
            fill=self.fill,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
        )


class CreateEllipseEdit(_Edit):
    """Create an ``<ellipse>`` — mirrors ``create_ellipse``."""

    op: Literal["create_ellipse"]
    cx: float
    cy: float
    rx: float
    ry: float
    parent_id: str | None = None
    object_id: str | None = None
    fill: str | None = None
    stroke: str | None = None
    stroke_width: str | None = None

    def build(self) -> MutateFn:
        return make_create_ellipse(
            self.cx,
            self.cy,
            self.rx,
            self.ry,
            parent_id=self.parent_id,
            object_id=self.object_id,
            fill=self.fill,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
        )


class CreateLineEdit(_Edit):
    """Create a ``<line>`` — mirrors ``create_line``."""

    op: Literal["create_line"]
    x1: float
    y1: float
    x2: float
    y2: float
    parent_id: str | None = None
    object_id: str | None = None
    stroke: str | None = None
    stroke_width: str | None = None

    def build(self) -> MutateFn:
        return make_create_line(
            self.x1,
            self.y1,
            self.x2,
            self.y2,
            parent_id=self.parent_id,
            object_id=self.object_id,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
        )


class CreatePolygonEdit(_Edit):
    """Create a closed ``<polygon>`` — mirrors ``create_polygon``."""

    op: Literal["create_polygon"]
    points: list[tuple[float, float]]
    parent_id: str | None = None
    object_id: str | None = None
    fill: str | None = None
    stroke: str | None = None
    stroke_width: str | None = None

    def build(self) -> MutateFn:
        return make_create_polygon(
            self.points,
            parent_id=self.parent_id,
            object_id=self.object_id,
            fill=self.fill,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
        )


class CreatePolylineEdit(_Edit):
    """Create an open ``<polyline>`` — mirrors ``create_polyline``."""

    op: Literal["create_polyline"]
    points: list[tuple[float, float]]
    parent_id: str | None = None
    object_id: str | None = None
    fill: str | None = None
    stroke: str | None = None
    stroke_width: str | None = None

    def build(self) -> MutateFn:
        return make_create_polyline(
            self.points,
            parent_id=self.parent_id,
            object_id=self.object_id,
            fill=self.fill,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
        )


class CreatePathEdit(_Edit):
    """Create a ``<path>`` — mirrors ``create_path``."""

    op: Literal["create_path"]
    d: str
    parent_id: str | None = None
    object_id: str | None = None
    fill: str | None = None
    stroke: str | None = None
    stroke_width: str | None = None

    def build(self) -> MutateFn:
        return make_create_path(
            self.d,
            parent_id=self.parent_id,
            object_id=self.object_id,
            fill=self.fill,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
        )


class CreateTextEdit(_Edit):
    """Create a ``<text>`` — mirrors ``create_text``."""

    op: Literal["create_text"]
    x: float
    y: float
    text: str
    parent_id: str | None = None
    object_id: str | None = None
    fill: str | None = None
    stroke: str | None = None
    stroke_width: str | None = None

    def build(self) -> MutateFn:
        return make_create_text(
            self.x,
            self.y,
            self.text,
            parent_id=self.parent_id,
            object_id=self.object_id,
            fill=self.fill,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
        )


class CreateGroupEdit(_Edit):
    """Create an empty ``<g>`` — mirrors ``create_group``."""

    op: Literal["create_group"]
    parent_id: str | None = None
    object_id: str | None = None

    def build(self) -> MutateFn:
        return make_create_group(parent_id=self.parent_id, object_id=self.object_id)


class GroupObjectsEdit(_Edit):
    """Wrap existing objects in a new ``<g>`` — mirrors ``group_objects``."""

    op: Literal["group_objects"]
    object_ids: list[str]
    object_id: str | None = None

    def build(self) -> MutateFn:
        return make_group_objects(self.object_ids, object_id=self.object_id)


class ReparentObjectEdit(_Edit):
    """Move an object under a new parent — mirrors ``reparent_object``."""

    op: Literal["reparent_object"]
    object_id: str
    new_parent_id: str

    def build(self) -> MutateFn:
        return make_reparent_object(self.object_id, self.new_parent_id)


class CreateUseEdit(_Edit):
    """Create a ``<use>`` reference — mirrors ``create_use``."""

    op: Literal["create_use"]
    href_id: str
    parent_id: str | None = None
    object_id: str | None = None
    x: float | None = None
    y: float | None = None
    transform: str | None = None

    def build(self) -> MutateFn:
        return make_create_use(
            self.href_id,
            parent_id=self.parent_id,
            object_id=self.object_id,
            x=self.x,
            y=self.y,
            transform=self.transform,
        )


class AddLinearGradientEdit(_Edit):
    """Add a ``<linearGradient>`` to ``<defs>`` — mirrors ``add_linear_gradient``."""

    op: Literal["add_linear_gradient"]
    stops: list[dict[str, object]]
    x1: str = "0%"
    y1: str = "0%"
    x2: str = "100%"
    y2: str = "0%"
    object_id: str | None = None

    def build(self) -> MutateFn:
        return make_add_linear_gradient(
            self.stops, x1=self.x1, y1=self.y1, x2=self.x2, y2=self.y2, object_id=self.object_id
        )


class AddRadialGradientEdit(_Edit):
    """Add a ``<radialGradient>`` to ``<defs>`` — mirrors ``add_radial_gradient``."""

    op: Literal["add_radial_gradient"]
    stops: list[dict[str, object]]
    cx: str = "50%"
    cy: str = "50%"
    r: str = "50%"
    fx: str | None = None
    fy: str | None = None
    object_id: str | None = None

    def build(self) -> MutateFn:
        return make_add_radial_gradient(
            self.stops,
            cx=self.cx,
            cy=self.cy,
            r=self.r,
            fx=self.fx,
            fy=self.fy,
            object_id=self.object_id,
        )


#: The discriminated union of every typed batch edit. FastMCP/pydantic select the concrete model by
#: the ``op`` tag, so a member is parsed and validated against its own schema before it is built.
TypedEdit = Annotated[
    SetFillEdit
    | SetStrokeEdit
    | SetOpacityEdit
    | ReplaceColorEdit
    | ApplyPaletteEdit
    | ReplaceTextEdit
    | SetFontEdit
    | DuplicateObjectEdit
    | RenameObjectEdit
    | DeleteObjectEdit
    | MoveObjectEdit
    | ScaleObjectEdit
    | RotateObjectEdit
    | ResizeCanvasEdit
    | NormalizeViewboxEdit
    | TileEdit
    | CreateRectEdit
    | CreateCircleEdit
    | CreateEllipseEdit
    | CreateLineEdit
    | CreatePolygonEdit
    | CreatePolylineEdit
    | CreatePathEdit
    | CreateTextEdit
    | CreateGroupEdit
    | GroupObjectsEdit
    | ReparentObjectEdit
    | CreateUseEdit
    | AddLinearGradientEdit
    | AddRadialGradientEdit,
    Field(discriminator="op"),
]


#: Validates + coerces a raw list (of dicts and/or models) into typed members, selecting each
#: member's model by its ``op`` discriminator. FastMCP already coerces over the wire, but a DIRECT
#: Python call (and the test suite) passes plain dicts — this makes both paths validate identically.
_BATCH_ADAPTER: TypeAdapter[list[TypedEdit]] = TypeAdapter(list[TypedEdit])


class BatchTooLarge(Exception):
    """The batch is empty or exceeds :data:`MAX_BATCH_EDITS`. Stable, host-path-free message."""


class InvalidBatchEdit(Exception):
    """A member failed schema validation (unknown ``op`` or bad field). Host-path-free message."""


def batch_risk(ops: list[TypedEdit]) -> RiskClass:
    """Effective risk of a batch = the MAX over its members (a ``high`` member escalates the batch).

    An empty batch is MEDIUM by convention (it is rejected earlier by :func:`build_batch`); a batch
    of style/transform/create edits is MEDIUM; any ``delete`` member makes the whole batch HIGH, so
    the kernel's policy gate demands the same ``approval_token`` that member would require alone.
    """
    risk = RiskClass.MEDIUM
    for op in ops:
        if _RISK_ORDER[op.RISK] > _RISK_ORDER[risk]:
            risk = op.RISK
    return risk


def coerce_edits(raw: list[Any]) -> list[TypedEdit]:
    """Validate + coerce a raw edit list into typed members, or raise :class:`InvalidBatchEdit`.

    Each item is matched to its concrete model by the ``op`` discriminator; an unknown ``op`` or a
    bad field raises with a concise, host-path-free message (pydantic errors carry field paths, not
    filesystem paths). Idempotent on already-typed members.
    """
    try:
        return _BATCH_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        raise InvalidBatchEdit(
            f"invalid edit in batch: {exc.error_count()} validation error(s)"
        ) from exc


def build_batch(ops: list[Any]) -> tuple[MutateFn, RiskClass, list[str]]:
    """Validate-all then return ONE ``mutate`` that applies the batch in order, plus its max risk.

    VALIDATE-ALL phase: every member's engine ``mutate`` closure is built UP FRONT, so any member
    whose value/schema is invalid (a bad colour, a non-finite number, a malformed id) raises
    ``EditError`` HERE — before the caller hands the closure to :func:`apply_edit`, i.e. before any
    Operation Record, snapshot, or write exists.

    APPLY phase: the returned ``mutate`` runs the members IN ORDER against the one in-memory tree
    the kernel passes it. The kernel writes the working copy only after ``mutate`` returns, so a
    member raising ``EditError`` / ``TargetNotFound`` mid-batch aborts the whole thing with the
    document byte-identical (all-or-nothing). The summary concatenates each member's own summary.

    Raises :class:`BatchTooLarge` for an empty or over-long batch and :class:`InvalidBatchEdit` for
    a member that fails schema validation.
    """
    if not ops:
        raise BatchTooLarge("apply_edits requires at least one edit")
    if len(ops) > MAX_BATCH_EDITS:
        raise BatchTooLarge(f"apply_edits batch exceeds {MAX_BATCH_EDITS} edits")

    typed = coerce_edits(ops)
    # Validate-all: build every member closure now so value/schema errors surface before apply_edit.
    members: list[tuple[str, MutateFn]] = [(op.op, op.build()) for op in typed]
    risk = batch_risk(typed)

    def mutate(tree: etree._ElementTree) -> str:
        summaries: list[str] = []
        for name, member in members:
            summaries.append(f"{name}: {member(tree)}")
        return f"applied {len(members)} edit(s) | " + " | ".join(summaries)

    return mutate, risk, [name for name, _ in members]


__all__ = [
    "MAX_BATCH_EDITS",
    "BatchTooLarge",
    "InvalidBatchEdit",
    "TypedEdit",
    "batch_risk",
    "build_batch",
    "coerce_edits",
]
