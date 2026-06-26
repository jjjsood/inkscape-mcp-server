"""Element-creation / defs / grouping tools.

Thin MCP layer over the direct-DOM engines in :mod:`inkscape_mcp.edit.create`. One small, typed
tool per shape / def / grouping primitive (no portmanteau `add_element(tag, attrs)` per ADR-002 /
ADR-003): the shape primitives (`create_rect` / `create_circle` / `create_ellipse` / `create_line`
/ `create_polygon` / `create_polyline` / `create_path` / `create_text`), the gradient builders
(`add_linear_gradient` / `add_radial_gradient`), and the structural builders (`create_group` /
`group_objects` / `reparent_object` / `create_use`).

Each tool builds the engine's ``mutate`` closure and hands it to the shared, reversible edit
pipeline (`apply_edit`), so every change is snapshotted, recorded as an Operation Record, and
linked to a before/after preview (ADR-004). Creation is direct lxml on the DOM only (ADR-005).

The pipeline returns an `EditResult`; the epic also requires the new `object_id` + a `bbox`. Each
tool therefore returns a `CreateResult` (an `EditResult` extended with `object_id` and an optional
`bbox`). The new id is captured from the live tree after the mutation; the bbox is computed
ANALYTICALLY for the simple shapes (rect / circle / ellipse / line / polygon / polyline) and is
`None` for shapes whose geometry is not analytically cheap (path, text) and for the gradient defs
(whose "object" is a def, not a drawn shape).

This layer maps engine / pipeline exceptions to `ToolError` with STABLE, host-path-free messages
(sec.12): an unknown document id becomes ``"document id not found"``, a missing object/parent id
becomes ``"object id not found in document"``, an unparseable working copy becomes ``"document
could not be parsed safely"``, and a validation failure surfaces its already-safe ``EditError``
message. Every tool is medium risk (write-new on the working copy, reversible).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from inkscape_mcp.document.inspect import DocumentNotFound, InspectionError
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
from inkscape_mcp.edit.dom import EditError, TargetNotFound
from inkscape_mcp.edit.pipeline import EditApplyError, EditResult, MutateFn, apply_edit
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.server import mcp

_logger = get_logger("tools.create")


class BBox(BaseModel):
    """An axis-aligned bounding box in the element's USER coordinate space (no transform applied).

    Computed analytically from the shape's geometry. Reported for the simple shapes (rect / circle /
    ellipse / line / polygon / polyline); `None` for path / text (geometry not analytically cheap)
    and for gradient defs (not a drawn object).
    """

    x: float
    y: float
    width: float
    height: float


class CreateResult(EditResult):
    """An :class:`EditResult` extended with the newly-created object's id and (when cheap) bbox.

    `object_id` is the id of the element / gradient def the call created (always present on a real
    change). `bbox` is the analytic user-space bounding box for the simple shapes, or `None` for
    path / text / gradient defs.
    """

    object_id: str
    bbox: BBox | None = None


class _Holder:
    """A tiny mutable cell the mutate closure writes the new id into so the tool layer can read it.

    The pipeline computes `changed` itself and only returns its own `EditResult` fields; to surface
    the new id we let `mutate` stash it here (the engine's summary already names it, but a dedicated
    field is cleaner than parsing the summary).
    """

    def __init__(self) -> None:
        self.object_id: str = ""


def _capture(holder: _Holder, inner: MutateFn) -> MutateFn:
    """Wrap an engine ``mutate`` so the id of the element it created is captured into ``holder``.

    The new element is the one whose id is absent from the tree BEFORE the mutation and present
    after — but the simplest robust capture is to record the id set during the mutation. The engine
    summaries embed the id in quotes (e.g. ``created <rect> 'rect-ab12'``); we re-derive the id from
    the tree by diffing ids, which needs no summary parsing and is exact.
    """

    def wrapped(tree: Any) -> str:
        before = {e.get("id") for e in tree.getroot().iter() if isinstance(e.tag, str)}
        summary = inner(tree)
        after = {e.get("id") for e in tree.getroot().iter() if isinstance(e.tag, str)}
        new_ids = [i for i in (after - before) if i]
        # group_objects / create_group / gradients add exactly one new id (the container/def);
        # the grouped children are MOVED, not created, so their ids are unchanged.
        if new_ids:
            holder.object_id = new_ids[0]
        return summary

    return wrapped


def _apply_create(
    doc_id: str,
    tool: str,
    params: dict[str, Any],
    build_mutate: Callable[[], MutateFn],
    bbox: BBox | None,
) -> CreateResult:
    """Run one creation through the pipeline, mapping engine errors to stable `ToolError`s.

    `build_mutate` constructs the engine's `mutate` closure; it is called INSIDE the try block so
    build-time validation (a bad numeric, a bad id, a malformed `d`, an injection-y colour) raises
    `EditError` here and maps to `ToolError` exactly like a validation failure during the mutation.
    The new object's id is captured from the live tree and folded into a `CreateResult` alongside
    the analytic `bbox`.
    """
    holder = _Holder()
    try:
        result = apply_edit(doc_id, tool, params, _capture(holder, build_mutate()))
    except (EditApplyError, DocumentNotFound, KeyError) as exc:
        raise ToolError("document id not found") from exc
    except TargetNotFound as exc:
        raise ToolError("object id not found in document") from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc
    except EditError as exc:
        raise ToolError(str(exc)) from exc
    return CreateResult(
        **result.model_dump(),
        object_id=holder.object_id,
        bbox=bbox if result.changed else None,
    )


def _rect_bbox(x: float, y: float, width: float, height: float) -> BBox:
    return BBox(x=x, y=y, width=width, height=height)


def _ellipse_bbox(cx: float, cy: float, rx: float, ry: float) -> BBox:
    return BBox(x=cx - rx, y=cy - ry, width=2 * rx, height=2 * ry)


def _points_bbox(points: list[tuple[float, float]]) -> BBox | None:
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return BBox(x=min(xs), y=min(ys), width=max(xs) - min(xs), height=max(ys) - min(ys))


# --- Shape primitives ----------------------------------------------


@mcp.tool
def create_rect(
    doc_id: str,
    x: float,
    y: float,
    width: float,
    height: float,
    parent_id: str | None = None,
    object_id: str | None = None,
    rx: float | None = None,
    ry: float | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
) -> CreateResult:
    """Create a `<rect>` at `(x, y)` sized `width` / `height` (> 0), with optional corner radii.

    When to use: drawing a rectangle / square / box. For an ellipse use `create_circle` /
    `create_ellipse`; for a freeform shape use `create_path`.

    Key params: `width` / `height` > 0; `rx` / `ry` optional corner radii; inserted into `parent_id`
    (must exist) or the document default parent (first layer, else root); `object_id` to pin the id.
    Optional `fill` / `stroke` / `stroke_width` paint the shape IN THIS CALL — validated
    exactly like `set_fill` / `set_stroke` (colour or `url(#id)`; CSS length) — so no mandatory
    second styling call (default None = unpainted, prior behaviour).

    Return shape: `CreateResult` — `object_id` (new id), analytic `bbox`, plus the pipeline fields
    (`operation_id`, `snapshot_id`, `changed`, before/after preview).

    Example: `create_rect(doc_id, 10, 10, 100, 60, rx=8, fill="#3366cc")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible write-new on the working copy; original untouched).
    """
    result = _apply_create(
        doc_id,
        "create_rect",
        {"x": x, "y": y, "width": width, "height": height, "parent_id": parent_id},
        lambda: make_create_rect(
            x,
            y,
            width,
            height,
            parent_id=parent_id,
            object_id=object_id,
            rx=rx,
            ry=ry,
            fill=fill,
            stroke=stroke,
            stroke_width=stroke_width,
        ),
        _rect_bbox(x, y, width, height),
    )
    log_tool_call(
        _logger,
        tool="create_rect",
        doc_id=doc_id,
        object_id=result.object_id,
        operation_id=result.operation_id,
    )
    return result


@mcp.tool
def create_circle(
    doc_id: str,
    cx: float,
    cy: float,
    r: float,
    parent_id: str | None = None,
    object_id: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
) -> CreateResult:
    """Create a `<circle>` centred at `(cx, cy)` with radius `r` (> 0).

    When to use: drawing a circle / disc. For an oval use `create_ellipse`; for a box use
    `create_rect`.

    Key params: `r` > 0; inserted into `parent_id` (must exist) or the document default parent;
    `object_id` to pin the id. Optional `fill` / `stroke` / `stroke_width` paint it in
    this call (validated like `set_fill` / `set_stroke`; default None = unpainted).

    Return shape: `CreateResult` — `object_id` (new id), analytic `bbox`, plus the pipeline fields
    (`operation_id`, `snapshot_id`, `changed`, before/after preview).

    Example: `create_circle(doc_id, 50, 50, 25, fill="red")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible write-new on the working copy; original untouched).
    """
    result = _apply_create(
        doc_id,
        "create_circle",
        {"cx": cx, "cy": cy, "r": r, "parent_id": parent_id},
        lambda: make_create_circle(
            cx,
            cy,
            r,
            parent_id=parent_id,
            object_id=object_id,
            fill=fill,
            stroke=stroke,
            stroke_width=stroke_width,
        ),
        _ellipse_bbox(cx, cy, r, r),
    )
    log_tool_call(
        _logger,
        tool="create_circle",
        doc_id=doc_id,
        object_id=result.object_id,
        operation_id=result.operation_id,
    )
    return result


@mcp.tool
def create_ellipse(
    doc_id: str,
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    parent_id: str | None = None,
    object_id: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
) -> CreateResult:
    """Create an `<ellipse>` centred at `(cx, cy)` with radii `rx` / `ry` (> 0).

    When to use: drawing an oval / ellipse. For a perfect circle use `create_circle`; for a box use
    `create_rect`.

    Key params: `rx` / `ry` > 0; inserted into `parent_id` (must exist) or the document default
    parent; `object_id` to pin the id. Optional `fill` / `stroke` / `stroke_width` paint
    it in this call (validated like `set_fill` / `set_stroke`; default None = unpainted).

    Return shape: `CreateResult` — `object_id` (new id), analytic `bbox`, plus the pipeline fields
    (`operation_id`, `snapshot_id`, `changed`, before/after preview).

    Example: `create_ellipse(doc_id, 50, 50, 30, 18, fill="#0a0")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible write-new on the working copy; original untouched).
    """
    result = _apply_create(
        doc_id,
        "create_ellipse",
        {"cx": cx, "cy": cy, "rx": rx, "ry": ry, "parent_id": parent_id},
        lambda: make_create_ellipse(
            cx,
            cy,
            rx,
            ry,
            parent_id=parent_id,
            object_id=object_id,
            fill=fill,
            stroke=stroke,
            stroke_width=stroke_width,
        ),
        _ellipse_bbox(cx, cy, rx, ry),
    )
    log_tool_call(
        _logger,
        tool="create_ellipse",
        doc_id=doc_id,
        object_id=result.object_id,
        operation_id=result.operation_id,
    )
    return result


@mcp.tool
def create_line(
    doc_id: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    parent_id: str | None = None,
    object_id: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
) -> CreateResult:
    """Create a `<line>` from `(x1, y1)` to `(x2, y2)`.

    When to use: a single straight segment. For a multi-segment open run use `create_polyline`; for
    a closed shape use `create_polygon`.

    Key params: endpoints `(x1, y1)` / `(x2, y2)`; inserted into `parent_id` (must exist) or the
    document default parent; `object_id` to pin the id. Optional `stroke` / `stroke_width`
    paint the segment in this call (a line is unfilled by nature, so no `fill`; validated like
    `set_stroke`; default None = unpainted).

    Return shape: `CreateResult` — `object_id` (new id), analytic `bbox` (the segment's axis-aligned
    extent), plus the pipeline fields (`operation_id`, `snapshot_id`, `changed`, preview).

    Example: `create_line(doc_id, 0, 0, 100, 100, stroke="black", stroke_width="2")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible write-new on the working copy; original untouched).
    """
    result = _apply_create(
        doc_id,
        "create_line",
        {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "parent_id": parent_id},
        lambda: make_create_line(
            x1,
            y1,
            x2,
            y2,
            parent_id=parent_id,
            object_id=object_id,
            stroke=stroke,
            stroke_width=stroke_width,
        ),
        _points_bbox([(x1, y1), (x2, y2)]),
    )
    log_tool_call(
        _logger,
        tool="create_line",
        doc_id=doc_id,
        object_id=result.object_id,
        operation_id=result.operation_id,
    )
    return result


@mcp.tool
def create_polygon(
    doc_id: str,
    points: list[tuple[float, float]],
    parent_id: str | None = None,
    object_id: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
) -> CreateResult:
    """Create a closed `<polygon>` from `points` (≥ 1 `(x, y)` pairs).

    When to use: a closed many-sided shape (triangle, hexagon, ...). For an OPEN run use
    `create_polyline`; for curves use `create_path`.

    Key params: `points` ≥ 1 `(x, y)` pairs; inserted into `parent_id` (must exist) or the document
    default parent; `object_id` to pin the id. Optional `fill` / `stroke` / `stroke_width`
    paint it in this call (validated like `set_fill` / `set_stroke`; default None = unpainted).

    Return shape: `CreateResult` — `object_id` (new id), analytic `bbox` (extent of the points),
    plus the pipeline fields (`operation_id`, `snapshot_id`, `changed`, preview).

    Example: `create_polygon(doc_id, [(0, 0), (50, 0), (25, 40)], fill="#fc0")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible write-new on the working copy; original untouched).
    """
    result = _apply_create(
        doc_id,
        "create_polygon",
        {"point_count": len(points), "parent_id": parent_id},
        lambda: make_create_polygon(
            points,
            parent_id=parent_id,
            object_id=object_id,
            fill=fill,
            stroke=stroke,
            stroke_width=stroke_width,
        ),
        _points_bbox(points),
    )
    log_tool_call(
        _logger,
        tool="create_polygon",
        doc_id=doc_id,
        object_id=result.object_id,
        operation_id=result.operation_id,
    )
    return result


@mcp.tool
def create_polyline(
    doc_id: str,
    points: list[tuple[float, float]],
    parent_id: str | None = None,
    object_id: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
) -> CreateResult:
    """Create an open `<polyline>` from `points` (≥ 1 `(x, y)` pairs).

    When to use: a connected open run of segments. For a CLOSED shape use `create_polygon`; for a
    single segment use `create_line`; for curves use `create_path`.

    Key params: `points` ≥ 1 `(x, y)` pairs; inserted into `parent_id` (must exist) or the document
    default parent; `object_id` to pin the id. Optional `fill` / `stroke` / `stroke_width`
    paint it in this call (validated like `set_fill` / `set_stroke`; default None = unpainted).

    Return shape: `CreateResult` — `object_id` (new id), analytic `bbox` (extent of the points),
    plus the pipeline fields (`operation_id`, `snapshot_id`, `changed`, preview).

    Example: `create_polyline(doc_id, [(0, 0), (50, 20), (100, 0)], stroke="blue")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible write-new on the working copy; original untouched).
    """
    result = _apply_create(
        doc_id,
        "create_polyline",
        {"point_count": len(points), "parent_id": parent_id},
        lambda: make_create_polyline(
            points,
            parent_id=parent_id,
            object_id=object_id,
            fill=fill,
            stroke=stroke,
            stroke_width=stroke_width,
        ),
        _points_bbox(points),
    )
    log_tool_call(
        _logger,
        tool="create_polyline",
        doc_id=doc_id,
        object_id=result.object_id,
        operation_id=result.operation_id,
    )
    return result


@mcp.tool
def create_path(
    doc_id: str,
    d: str,
    parent_id: str | None = None,
    object_id: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
) -> CreateResult:
    """Create a `<path>` with the validated `d` data string.

    When to use: freeform / bezier / curve geometry from a `d` string. For simple primitives prefer
    `create_rect` / `create_circle` / `create_polygon`; to edit an existing path's geometry use the
    `paths` tools (`simplify_path`, `combine_paths`, ...).

    Key params: `d` validated against a strict charset (digits, whitespace, `,`, `.`, sign,
    exponent, SVG path command letters only) and length-bounded; geometry is NOT fully parsed; into
    `parent_id` (must exist) or the document default parent. Optional `fill` / `stroke` /
    `stroke_width` paint it in this call (validated like `set_fill` / `set_stroke`;
    default None = unpainted).

    Return shape: `CreateResult` — `object_id` (new id), `bbox=None` (paths are not analytically
    measured), plus the pipeline fields (`operation_id`, `snapshot_id`, `changed`, preview).

    Example: `create_path(doc_id, "M0 0 L100 0 L50 80 Z", fill="#222")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible write-new on the working copy; original untouched).
    """
    result = _apply_create(
        doc_id,
        "create_path",
        {"d_length": len(d), "parent_id": parent_id},
        lambda: make_create_path(
            d,
            parent_id=parent_id,
            object_id=object_id,
            fill=fill,
            stroke=stroke,
            stroke_width=stroke_width,
        ),
        None,
    )
    log_tool_call(
        _logger,
        tool="create_path",
        doc_id=doc_id,
        object_id=result.object_id,
        operation_id=result.operation_id,
    )
    return result


@mcp.tool
def create_text(
    doc_id: str,
    x: float,
    y: float,
    text: str,
    parent_id: str | None = None,
    object_id: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
) -> CreateResult:
    """Create a `<text>` element anchored at `(x, y)` holding `text`.

    When to use: adding a new label / caption. To change EXISTING text content use `replace_text`;
    to restyle its font use `set_font`.

    Key params: `text` is length-bounded and rejects control characters other than tab / newline /
    carriage return (stored as a text node, no markup injection); inserted into `parent_id` (must
    exist) or the document default parent. Optional `fill` / `stroke` / `stroke_width`
    paint the glyphs in this call (validated like `set_fill` / `set_stroke`; default None =
    unpainted). For font family/size/weight use `set_font`.

    Return shape: `CreateResult` — `object_id` (new id), `bbox=None` (text is not analytically
    measured), plus the pipeline fields (`operation_id`, `snapshot_id`, `changed`, preview).

    Example: `create_text(doc_id, 20, 40, "Hello", fill="#111")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible write-new on the working copy; original untouched).
    """
    result = _apply_create(
        doc_id,
        "create_text",
        {"x": x, "y": y, "text_length": len(text), "parent_id": parent_id},
        lambda: make_create_text(
            x,
            y,
            text,
            parent_id=parent_id,
            object_id=object_id,
            fill=fill,
            stroke=stroke,
            stroke_width=stroke_width,
        ),
        None,
    )
    log_tool_call(
        _logger,
        tool="create_text",
        doc_id=doc_id,
        object_id=result.object_id,
        operation_id=result.operation_id,
    )
    return result


# --- defs / gradients ----------------------------------------------


@mcp.tool
def add_linear_gradient(
    doc_id: str,
    stops: list[dict[str, Any]],
    x1: str = "0%",
    y1: str = "0%",
    x2: str = "100%",
    y2: str = "0%",
    object_id: str | None = None,
) -> CreateResult:
    """Add a `<linearGradient>` to the document `<defs>` (created if absent).

    When to use: defining a directional colour fade to paint with. For a centred/radial fade use
    `add_radial_gradient`; after defining, apply it via `set_fill(doc_id, ids, "url(#grad-id)")`.

    Key params: `stops` is a list of `{offset, color, opacity?}` (≥ 1): `offset` a 0..1 number or
    0%..100% percentage, `color` a validated colour, optional `opacity` in [0, 1]. The vector runs
    `(x1, y1) -> (x2, y2)` (numbers or percentages; default a left-to-right sweep).

    Return shape: `CreateResult` — `object_id` is the gradient id (use as `url(#id)` paint),
    `bbox=None` (a def, not a drawn shape), plus the pipeline fields.

    Example: `add_linear_gradient(doc_id, [{"offset": 0, "color": "#fff"}, {"offset": 1,
    "color": "#3366cc"}])`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible write-new on the working copy; original untouched).
    """
    result = _apply_create(
        doc_id,
        "add_linear_gradient",
        {"stop_count": len(stops), "x1": x1, "y1": y1, "x2": x2, "y2": y2},
        lambda: make_add_linear_gradient(stops, x1=x1, y1=y1, x2=x2, y2=y2, object_id=object_id),
        None,
    )
    log_tool_call(
        _logger,
        tool="add_linear_gradient",
        doc_id=doc_id,
        object_id=result.object_id,
        operation_id=result.operation_id,
    )
    return result


@mcp.tool
def add_radial_gradient(
    doc_id: str,
    stops: list[dict[str, Any]],
    cx: str = "50%",
    cy: str = "50%",
    r: str = "50%",
    fx: str | None = None,
    fy: str | None = None,
    object_id: str | None = None,
) -> CreateResult:
    """Add a `<radialGradient>` to the document `<defs>` (created if absent).

    When to use: defining a centred/radial colour fade to paint with. For a directional fade use
    `add_linear_gradient`; after defining, apply via `set_fill(doc_id, ids, "url(#gradient-id)")`.

    Key params: `stops` is a list of `{offset, color, opacity?}` (≥ 1), as for the linear gradient.
    Centred at `(cx, cy)` with radius `r` (numbers or percentages; default a centred 50% circle);
    `fx` / `fy` optionally set the focal point.

    Return shape: `CreateResult` — `object_id` is the gradient id (use as `url(#id)` paint),
    `bbox=None`, plus the pipeline fields.

    Example: `add_radial_gradient(doc_id, [{"offset": 0, "color": "#fff"}, {"offset": 1,
    "color": "#000"}])`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible write-new on the working copy; original untouched).
    """
    result = _apply_create(
        doc_id,
        "add_radial_gradient",
        {"stop_count": len(stops), "cx": cx, "cy": cy, "r": r},
        lambda: make_add_radial_gradient(
            stops, cx=cx, cy=cy, r=r, fx=fx, fy=fy, object_id=object_id
        ),
        None,
    )
    log_tool_call(
        _logger,
        tool="add_radial_gradient",
        doc_id=doc_id,
        object_id=result.object_id,
        operation_id=result.operation_id,
    )
    return result


# --- grouping / symbols --------------------------------------------


@mcp.tool
def create_group(
    doc_id: str,
    parent_id: str | None = None,
    object_id: str | None = None,
) -> CreateResult:
    """Create an empty `<g>` group inside `parent_id` (must exist) or the document default parent.

    When to use: making an EMPTY group to populate later. To wrap EXISTING objects in a new group
    use `group_objects`; to move one object into an existing group use `reparent_object`.

    Key params: `parent_id` (must exist) or the document default parent; `object_id` to pin the id.

    Return shape: `CreateResult` — `object_id` is the new group id, `bbox=None` (empty), plus the
    pipeline fields (`operation_id`, `snapshot_id`, `changed`, preview).

    Example: `create_group(doc_id)`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible write-new on the working copy; original untouched).
    """
    result = _apply_create(
        doc_id,
        "create_group",
        {"parent_id": parent_id},
        lambda: make_create_group(parent_id=parent_id, object_id=object_id),
        None,
    )
    log_tool_call(
        _logger,
        tool="create_group",
        doc_id=doc_id,
        object_id=result.object_id,
        operation_id=result.operation_id,
    )
    return result


@mcp.tool
def group_objects(
    doc_id: str,
    object_ids: list[str],
    object_id: str | None = None,
) -> CreateResult:
    """Wrap existing objects (`object_ids`, ≥ 1, all must exist) in a NEW `<g>`.

    When to use: collecting several existing objects under one group. For an EMPTY group use
    `create_group`; to move a single object into an existing group use `reparent_object`.

    Key params: `object_ids` ≥ 1, all must exist; `object_id` to pin the new group id. The objects
    keep their own transforms / styles; only their parent changes.

    Return shape: `CreateResult` — `object_id` is the new group id (inserted at the position of the
    first target), `bbox=None`, plus the pipeline fields.

    Example: `group_objects(doc_id, ["icon", "label"])`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible write-new on the working copy; original untouched).
    """
    result = _apply_create(
        doc_id,
        "group_objects",
        {"object_ids": object_ids},
        lambda: make_group_objects(object_ids, object_id=object_id),
        None,
    )
    log_tool_call(
        _logger,
        tool="group_objects",
        doc_id=doc_id,
        object_id=result.object_id,
        operation_id=result.operation_id,
    )
    return result


@mcp.tool
def reparent_object(
    doc_id: str,
    object_id: str,
    new_parent_id: str,
) -> CreateResult:
    """Move an object (`object_id`) under a new parent (`new_parent_id`); both must exist.

    When to use: re-nesting one existing object into another group. To wrap SEVERAL objects in a new
    group use `group_objects`; to reposition without re-nesting use `move_object`.

    Key params: `object_id` and `new_parent_id` both must exist; rejected if the new parent is the
    object itself or one of its descendants. NOTE: re-parenting changes the inherited coordinate
    space — the object's visual position can shift if old/new parents carry different transforms.

    Return shape: `CreateResult` — `object_id` echoes the moved object, `bbox=None`, plus the
    pipeline fields (`operation_id`, `snapshot_id`, `changed`, preview).

    Example: `reparent_object(doc_id, "star", "layer2")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible edit on the working copy; original untouched).
    """
    result = _apply_create(
        doc_id,
        "reparent_object",
        {"object_id": object_id, "new_parent_id": new_parent_id},
        lambda: make_reparent_object(object_id, new_parent_id),
        None,
    )
    # reparent moves an existing object (no new id is minted), so echo the moved object's id.
    moved = CreateResult(**result.model_dump(exclude={"object_id", "bbox"}), object_id=object_id)
    log_tool_call(
        _logger,
        tool="reparent_object",
        doc_id=doc_id,
        object_id=object_id,
        operation_id=result.operation_id,
    )
    return moved


@mcp.tool
def create_use(
    doc_id: str,
    href_id: str,
    parent_id: str | None = None,
    object_id: str | None = None,
    x: float | None = None,
    y: float | None = None,
    transform: str | None = None,
) -> CreateResult:
    """Create a `<use href="#href_id">` referencing an existing same-document object.

    When to use: instancing / cloning an existing element so edits to the original propagate. To
    deep-COPY (independent) use `duplicate_object`; to grid-repeat use `tile`.

    Key params: `href_id` MUST name an existing element (safe-id charset, required to exist);
    external / `javascript:` / `url(...)` references are rejected — only a same-document `#id`. Into
    `parent_id` (must exist) or the document default parent. Placement (translate-scaling trap):
    `<use>` applies `x` / `y` as a translation BEFORE its `transform`, so `scale(2)` + `x="10"`
    shifts by 20 — prefer EITHER `x` / `y` alone OR fold the translation into `transform`
    (e.g. `translate(10,0) scale(2)`); do not mix `x` / `y` with a scaling `transform`.

    Return shape: `CreateResult` — `object_id` is the new `<use>` id, `bbox=None`, plus the pipeline
    fields (`operation_id`, `snapshot_id`, `changed`, preview).

    Example: `create_use(doc_id, "logo", x=200, y=0)`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible write-new on the working copy; original untouched).
    """
    result = _apply_create(
        doc_id,
        "create_use",
        {"href_id": href_id, "parent_id": parent_id, "x": x, "y": y},
        lambda: make_create_use(
            href_id, parent_id=parent_id, object_id=object_id, x=x, y=y, transform=transform
        ),
        None,
    )
    log_tool_call(
        _logger,
        tool="create_use",
        doc_id=doc_id,
        object_id=result.object_id,
        operation_id=result.operation_id,
    )
    return result
