"""Transform tools (E2-03): direct-DOM, reversible, medium risk.

Small typed tools (ADR-002, no portmanteau) — ``move_object``, ``scale_object``,
``rotate_object``, ``resize_canvas``, ``normalize_viewbox``, ``fit_to_content``, ``tile`` — that
each apply ONE transform to a document's working copy. The simple transforms edit via direct lxml
DOM (ADR-005); ``fit_to_content`` queries the CONTENT bounding box through the Inkscape engine
(ADR-005 — real geometry, not naive XML). Every call routes through the shared edit pipeline
(:func:`inkscape_mcp.edit.pipeline.apply_edit`), so each change is uniformly snapshotted, recorded
as an Operation Record, and linked to a before/after preview (ADR-004).

``changed`` is HONEST and inherited DIRECTLY from the pipeline (:func:`apply_edit`), which is the
single source of truth: it canonical-serializes the working tree before and after the mutation and
returns ``changed=False`` for a genuine no-op (e.g. ``normalize_viewbox`` on an already-normal
doc), writing NO snapshot and NO Operation Record. This layer no longer re-derives ``changed`` — it
trusts the pipeline result verbatim.

This is a THIN layer: it builds the ``mutate`` closure over the pure engine in
``inkscape_mcp.edit.transform``, calls ``apply_edit``, and maps engine exceptions to
:class:`fastmcp.exceptions.ToolError` with stable, host-path-free messages (sec.12).
"""

from __future__ import annotations

from pathlib import Path

from fastmcp.exceptions import ToolError
from lxml import etree

from inkscape_mcp.document.inspect import DocumentNotFound, InspectionError
from inkscape_mcp.edit import transform as engine
from inkscape_mcp.edit.dom import EditError, TargetNotFound
from inkscape_mcp.edit.pipeline import EditApplyError, EditResult, MutateFn, apply_edit
from inkscape_mcp.edit.transform import ContentBBoxError
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.registry import Registry, get_registry
from inkscape_mcp.server import mcp

_logger = get_logger("tools.transform")


def _working_path(doc_id: str, registry: Registry | None) -> Path | None:
    """Resolve the working-copy path for ``doc_id``, or ``None`` for an unknown id.

    Used by ``fit_to_content`` to hand the engine the working copy for its content-bbox query.
    """
    reg = registry if registry is not None else get_registry()
    try:
        return Path(reg.get(doc_id).working_path)
    except KeyError:
        return None


def _apply(doc_id: str, tool: str, params: dict[str, object], mutate: MutateFn) -> EditResult:
    """Run one edit through the pipeline, mapping engine exceptions to stable `ToolError`s.

    ``changed`` comes straight from the pipeline result (the single source of truth — it returns
    ``changed=False`` with no snapshot/record for a genuine no-op). This layer adds no diff logic.

    Error mapping (sec.12, host-path-free):

    - unknown document (`EditApplyError`/`DocumentNotFound`/`KeyError`) -> "document id not found"
    - missing target object (`TargetNotFound`) -> "object id not found in document"
    - content bbox unavailable (`ContentBBoxError`) -> its already-safe engine message
    - invalid input (`EditError`) -> its already-safe validation message
    - unparseable working copy (`InspectionError`) -> "document could not be parsed safely"
    """
    try:
        result = apply_edit(doc_id, tool, params, mutate)
    except (EditApplyError, DocumentNotFound, KeyError) as exc:
        raise ToolError("document id not found") from exc
    except TargetNotFound as exc:
        raise ToolError("object id not found in document") from exc
    except ContentBBoxError as exc:
        raise ToolError(str(exc)) from exc
    except EditError as exc:
        raise ToolError(str(exc)) from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc

    log_tool_call(
        _logger,
        tool=tool,
        doc_id=doc_id,
        operation_id=result.operation_id,
        snapshot_id=result.snapshot_id,
    )
    return result


@mcp.tool
def move_object(doc_id: str, object_id: str, dx: float, dy: float) -> EditResult:
    """Translate an object/group by ``(dx, dy)`` in its parent coordinate space.

    When to use: repositioning one object; get its id from `find_objects`. To resize use
    `scale_object`, to spin use `rotate_object`, to lay out copies use `tile`.

    Key params: `dx`/`dy` are a delta in the parent coordinate space; a `translate(dx,dy)` is
    prepended to the target's transform (child geometry untouched).

    Return shape: `EditResult` — `operation_id`, `snapshot_id`, `changed` (real before/after content
    diff), before/after preview; the edit lands on the working copy only (reversible).

    Example: `move_object(doc_id, "logo", 10, 0)`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium.
    """

    def mutate(tree: etree._ElementTree) -> str:
        return engine.move(tree, object_id, dx, dy)

    return _apply(
        doc_id,
        "move_object",
        {"object_id": object_id, "dx": dx, "dy": dy},
        mutate,
    )


@mcp.tool
def scale_object(doc_id: str, object_id: str, sx: float, sy: float | None = None) -> EditResult:
    """Scale an object/group by factor ``sx`` (and ``sy``, defaulting to ``sx`` for uniform).

    When to use: resizing one object; get its id from `find_objects`. To reposition use
    `move_object`, to rotate use `rotate_object`, to resize the whole page use `resize_canvas`.

    Key params: `sx` (and optional `sy`, defaulting to `sx` for uniform) scale about the parent
    coordinate-space ORIGIN; non-finite or non-positive factors are rejected.

    Return shape: `EditResult` — `operation_id`, `snapshot_id`, `changed` (real before/after content
    diff), before/after preview; the edit lands on the working copy only (reversible).

    Example: `scale_object(doc_id, "logo", 2)`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium.
    """

    def mutate(tree: etree._ElementTree) -> str:
        return engine.scale(tree, object_id, sx, sy)

    return _apply(
        doc_id,
        "scale_object",
        {"object_id": object_id, "sx": sx, "sy": sy},
        mutate,
    )


@mcp.tool
def rotate_object(
    doc_id: str,
    object_id: str,
    degrees: float,
    cx: float | None = None,
    cy: float | None = None,
) -> EditResult:
    """Rotate an object/group by ``degrees`` about a point.

    When to use: spinning one object; get its id from `find_objects`. To move use `move_object`, to
    resize use `scale_object`.

    Key params: `degrees` is the rotation angle; it rotates about `(cx, cy)` when BOTH are given,
    otherwise about the parent coordinate-space origin.

    Return shape: `EditResult` — `operation_id`, `snapshot_id`, `changed` (real before/after content
    diff), before/after preview; the edit lands on the working copy only (reversible).

    Example: `rotate_object(doc_id, "arrow", 90, cx=50, cy=50)`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium.
    """

    def mutate(tree: etree._ElementTree) -> str:
        return engine.rotate(tree, object_id, degrees, cx, cy)

    return _apply(
        doc_id,
        "rotate_object",
        {"object_id": object_id, "degrees": degrees, "cx": cx, "cy": cy},
        mutate,
    )


@mcp.tool
def resize_canvas(
    doc_id: str,
    width: str,
    height: str,
    adjust_viewbox: bool = False,
    bleed: float | None = None,
    bleed_color: str = "#ffffff",
) -> EditResult:
    """Set the document canvas ``width`` / ``height`` to validated CSS lengths.

    When to use: changing the PAGE size. To crop the page to the art use `fit_to_content`, to repair
    the viewBox use `normalize_viewbox`, to resize one OBJECT use `scale_object`.

    Key params: `width`/`height` are validated CSS lengths; child geometry is not altered. By
    default an existing `viewBox` is preserved (synthesized only when absent). `adjust_viewbox=True`
    RETARGETS the `viewBox` to `"0 0 W H"` so it tracks the new canvas (opt-in; changes the
    coordinate system). BLEED (opt-in, E16-10d): `bleed` > 0 ALSO grows the viewBox outward by that
    many user units on every side and paints the new border strip with `bleed_color` (validated
    colour, default white) via one background `<rect>` behind all content — a print-bleed resize in
    ONE call instead of a second `scale_object`/background step. `bleed` needs a valid existing or
    derivable viewBox and is mutually exclusive with `adjust_viewbox`.

    Return shape: `EditResult` — `operation_id`, `snapshot_id`, `changed` (real before/after content
    diff), before/after preview; the edit lands on the working copy only (reversible).

    Example: `resize_canvas(doc_id, "800", "600")`; with bleed:
    `resize_canvas(doc_id, "800", "600", bleed=8, bleed_color="#fff")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium.
    """

    def mutate(tree: etree._ElementTree) -> str:
        return engine.resize_canvas(
            tree, width, height, adjust_viewbox, bleed=bleed, bleed_color=bleed_color
        )

    return _apply(
        doc_id,
        "resize_canvas",
        {
            "width": width,
            "height": height,
            "adjust_viewbox": adjust_viewbox,
            "bleed": bleed,
            "bleed_color": bleed_color,
        },
        mutate,
    )


@mcp.tool
def normalize_viewbox(doc_id: str) -> EditResult:
    """Normalize or repair the document's root ``viewBox``.

    When to use: tidying/repairing a missing or malformed root `viewBox`. To frame the page to the
    art use `fit_to_content`, to set the page size use `resize_canvas`.

    Key params: none beyond `doc_id`. A valid 4-number `viewBox` is left unchanged (idempotent →
    `changed=False`); an absent one is synthesized from numeric `width`/`height`; a malformed one is
    repaired from width/height when possible.

    Return shape: `EditResult` — `operation_id`, `snapshot_id`, `changed` (real before/after content
    diff), before/after preview; the edit lands on the working copy only (reversible).

    Example: `normalize_viewbox(doc_id)`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium.
    """

    def mutate(tree: etree._ElementTree) -> str:
        return engine.normalize_viewbox(tree)

    return _apply(doc_id, "normalize_viewbox", {}, mutate)


@mcp.tool
def fit_to_content(doc_id: str) -> EditResult:
    """Fit the document's root ``viewBox`` to its CONTENT bounding box.

    When to use: cropping the page so it frames the drawing exactly. To set an explicit page size
    use `resize_canvas`, to merely repair the viewBox use `normalize_viewbox`.

    Key params: none beyond `doc_id`. The content bbox is computed by the Inkscape engine
    (`--query-all`, ADR-005 — real geometry, not naive XML) in the document's intrinsic
    user-coordinate space (probed against a px-identity copy so the value is STABLE across calls);
    only the root `viewBox` changes. IDEMPOTENT: a second call on an already-fitted document reports
    `changed=False`. Fails with a stable error if the engine is unavailable, the document has no
    drawable content, or the bbox is degenerate.

    Return shape: `EditResult` — `operation_id`, `snapshot_id`, `changed` (real before/after content
    diff), before/after preview; the edit lands on the working copy only (reversible).

    Example: `fit_to_content(doc_id)`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium.
    """
    working_path = _working_path(doc_id, None)

    def mutate(tree: etree._ElementTree) -> str:
        if working_path is None:  # pragma: no cover - unknown id fails earlier in apply_edit
            raise ContentBBoxError("document id not found")
        return engine.fit_to_content(tree, working_path)

    return _apply(doc_id, "fit_to_content", {}, mutate)


@mcp.tool
def tile(
    doc_id: str,
    object_id: str,
    rows: int,
    cols: int,
    dx: float,
    dy: float,
) -> EditResult:
    """Lay out a ``rows`` x ``cols`` grid of an object in ONE reversible operation.

    When to use: repeating one object into a grid in a single call. To move/scale/rotate one object
    use `move_object` / `scale_object` / `rotate_object`; to copy once use `duplicate_object`.

    Key params: the target stays as the `(0,0)` cell; `rows*cols - 1` deep copies (each re-id'd
    uniquely, intra-clone refs rewritten) are inserted, the copy at `(r, c)` translated by
    `(c*dx, r*dy)`. `rows`/`cols` must each be `>= 1` and their product must not exceed the engine's
    tile cap; `dx`/`dy` must be finite. A 4x4 grid is one call (not 30).

    Return shape: `EditResult` — `operation_id`, `snapshot_id`, `changed` (a 1x1 tile reports
    `changed=False`), before/after preview; the whole grid lands under one snapshot (reversible).

    Example: `tile(doc_id, "dot", 4, 4, 20, 20)`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium.
    """

    def mutate(tree: etree._ElementTree) -> str:
        return engine.tile(tree, object_id, rows, cols, dx, dy)

    return _apply(
        doc_id,
        "tile",
        {"object_id": object_id, "rows": rows, "cols": cols, "dx": dx, "dy": dy},
        mutate,
    )
