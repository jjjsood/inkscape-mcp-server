"""Style tools (E2-01): `set_fill` / `set_stroke` / `set_opacity` / `replace_color` /
`apply_palette`.

Thin MCP layer over the direct-DOM style engine (`inkscape_mcp.edit.style`). Each tool is small
and typed (ADR-002, no portmanteau): it validates the trivial argument shape, builds the engine's
`mutate` closure, and hands it to the shared edit pipeline (`apply_edit`) so every change is
uniformly reversible and audited — a pre-mutation snapshot plus an Operation Record with a linked
before/after preview (ADR-004). All edits are direct DOM via lxml (ADR-005); no Inkscape engine
is invoked. Policy classifies these as **medium** risk (write-new style on the working copy).

`changed` is inherited DIRECTLY from the pipeline (the single source of truth): a zero-effect
style edit — `replace_color` matching nothing, `apply_palette` whose colours appear nowhere,
`set_fill` to the colour already present — is detected by the pipeline's canonical before/after
diff, which returns `changed=False` and writes NO snapshot and NO Operation Record. This layer
adds no diff logic of its own.

Client-facing errors are raised as `ToolError` with stable, host-path-free messages (sec.12):
unknown document id -> "document id not found"; missing object id -> "object id not found in
document"; an unparseable working copy -> "document could not be parsed safely"; an invalid edit
input (bad colour / length / opacity) -> the validation message (already safe — built from
typed parameters with no host path).
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from inkscape_mcp.document.inspect import DocumentNotFound, InspectionError
from inkscape_mcp.edit.dom import EditError, TargetNotFound
from inkscape_mcp.edit.pipeline import EditApplyError, EditResult, apply_edit
from inkscape_mcp.edit.style import (
    apply_palette_mutate,
    replace_color_mutate,
    set_fill_mutate,
    set_opacity_mutate,
    set_stroke_mutate,
)
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.server import mcp

_logger = get_logger("tools.style")

#: Exceptions raised by the engine / pipeline that this layer maps to a stable `ToolError`.
_MAPPED = (
    EditApplyError,
    DocumentNotFound,
    KeyError,
    TargetNotFound,
    InspectionError,
    EditError,
)


def _map_failure(exc: Exception) -> ToolError:
    """Map an engine/pipeline exception to a stable, host-path-free `ToolError` (sec.12)."""
    if isinstance(exc, (EditApplyError, DocumentNotFound, KeyError)):
        return ToolError("document id not found")
    if isinstance(exc, TargetNotFound):
        return ToolError("object id not found in document")
    if isinstance(exc, InspectionError):
        return ToolError("document could not be parsed safely")
    if isinstance(exc, EditError):
        # Validation message, built from typed parameters — already safe (no host path).
        return ToolError(str(exc))
    return ToolError("style edit failed")


@mcp.tool
def set_fill(
    doc_id: str,
    object_ids: list[str],
    color: str,
    opacity: float | None = None,
) -> EditResult:
    """Set the fill colour (and optional fill opacity) of one or more objects.

    When to use: recolouring specific objects' fill (get the ids from `find_objects`). To change
    every instance of a colour document-wide use `replace_color`; for a whole theme use
    `apply_palette`; for the outline use `set_stroke`.

    Key params: `color` accepts hex, `rgb()/rgba()/hsl()/hsla()`, a named colour, or a `url(#id)`
    paint-server reference — a gradient/pattern in `<defs>`, e.g. an id from `add_linear_gradient` /
    `add_radial_gradient` (optionally with a fallback colour: `url(#id) red`). External urls,
    `javascript:`, and CSS-injection punctuation are rejected. `opacity`, if given, in [0, 1].

    Return shape: `EditResult` — `operation_id`, `snapshot_id`, `changed` (false if the colour was
    already present), before/after preview; the edit lands on the working copy only (reversible).

    Example: `set_fill(doc_id, ["logo"], "#3366cc")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium.
    """
    try:
        mutate = set_fill_mutate(object_ids, color, opacity)
        result = apply_edit(
            doc_id,
            "set_fill",
            {"object_ids": object_ids, "color": color, "opacity": opacity},
            mutate,
        )
    except _MAPPED as exc:
        raise _map_failure(exc) from exc

    log_tool_call(_logger, tool="set_fill", doc_id=doc_id, operation_id=result.operation_id)
    return result


@mcp.tool
def set_stroke(
    doc_id: str,
    object_ids: list[str],
    color: str | None = None,
    width: str | None = None,
    opacity: float | None = None,
) -> EditResult:
    """Set the stroke colour, width, and/or opacity of one or more objects.

    When to use: styling an object's outline/border. For the interior use `set_fill`; to turn a
    stroke into a filled outline path use `stroke_to_path`.

    Key params: at least one of `color`, `width`, `opacity` must be supplied. `color` accepts a
    colour OR a `url(#id)` paint-server reference (gradient/pattern in `<defs>`, optionally with a
    fallback colour); `width` is a CSS length (number + optional unit); `opacity` must be in [0, 1].
    External urls, `javascript:`, and CSS-injection punctuation are rejected.

    Return shape: `EditResult` — `operation_id`, `snapshot_id`, `changed`, before/after preview; the
    edit lands on the working copy only (reversible).

    Example: `set_stroke(doc_id, ["border"], color="#000", width="2")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium.
    """
    try:
        mutate = set_stroke_mutate(object_ids, color, width, opacity)
        result = apply_edit(
            doc_id,
            "set_stroke",
            {
                "object_ids": object_ids,
                "color": color,
                "width": width,
                "opacity": opacity,
            },
            mutate,
        )
    except _MAPPED as exc:
        raise _map_failure(exc) from exc

    log_tool_call(_logger, tool="set_stroke", doc_id=doc_id, operation_id=result.operation_id)
    return result


@mcp.tool
def set_opacity(doc_id: str, object_ids: list[str], opacity: float) -> EditResult:
    """Set the element-level opacity of one or more objects.

    When to use: making whole objects more/less transparent. For fill-only or stroke-only opacity
    use `set_fill` / `set_stroke` with their `opacity` argument instead.

    Key params: `opacity` must be in [0, 1] (this is the element `opacity`, affecting fill AND
    stroke together).

    Return shape: `EditResult` — `operation_id`, `snapshot_id`, `changed`, before/after preview; the
    edit lands on the working copy only (reversible).

    Example: `set_opacity(doc_id, ["overlay"], 0.5)`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium.
    """
    try:
        mutate = set_opacity_mutate(object_ids, opacity)
        result = apply_edit(
            doc_id,
            "set_opacity",
            {"object_ids": object_ids, "opacity": opacity},
            mutate,
        )
    except _MAPPED as exc:
        raise _map_failure(exc) from exc

    log_tool_call(_logger, tool="set_opacity", doc_id=doc_id, operation_id=result.operation_id)
    return result


@mcp.tool
def replace_color(
    doc_id: str,
    from_color: str,
    to_color: str,
    scope_ids: list[str] | None = None,
) -> EditResult:
    """Replace one colour with another across the document (or within `scope_ids` subtrees).

    When to use: swapping every occurrence of one colour for another. For a multi-colour theme swap
    use `apply_palette`; to recolour specific objects only use `set_fill` / `set_stroke`.

    Key params: both colours are validated; matching is case- and hex-shorthand-insensitive and
    covers inline-style colour properties (`fill`, `stroke`, `stop-color`, ...) and the same-named
    presentation attributes. `scope_ids`, if given, confines the replacement to those elements'
    subtrees (each id must exist).

    Return shape: `EditResult` — `operation_id`, `snapshot_id`, `changed` (false if the colour was
    not found anywhere in scope), before/after preview; lands on the working copy only (reversible).

    Example: `replace_color(doc_id, "#ff0000", "#3366cc")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium.
    """
    try:
        mutate = replace_color_mutate(from_color, to_color, scope_ids)
        result = apply_edit(
            doc_id,
            "replace_color",
            {"from_color": from_color, "to_color": to_color, "scope_ids": scope_ids},
            mutate,
        )
    except _MAPPED as exc:
        raise _map_failure(exc) from exc

    log_tool_call(_logger, tool="replace_color", doc_id=doc_id, operation_id=result.operation_id)
    return result


@mcp.tool
def apply_palette(
    doc_id: str,
    mapping: dict[str, str],
    scope_ids: list[str] | None = None,
) -> EditResult:
    """Apply many `from -> to` colour replacements in a single reversible operation.

    When to use: re-theming / rebranding a document's colours in one shot. For a single colour swap
    use `replace_color`; to recolour specific objects use `set_fill` / `set_stroke`.

    Key params: `mapping` maps each source colour to its replacement; every key AND value is
    strictly colour-validated UP FRONT — a typo'd or non-colour entry (e.g. `notacolor`) is rejected
    with a `ToolError` BEFORE any mutation, op record, or snapshot is created (E10-02). Each reuses
    the `replace_color` matching logic. `scope_ids`, if given, confines all replacements to those
    elements' subtrees.

    Return shape: `EditResult` — `operation_id`, `snapshot_id`, `changed` (a real before/after
    content diff), before/after preview; the whole palette is applied under one snapshot
    (reversible).

    Example: `apply_palette(doc_id, {"#ff0000": "#3366cc", "#00ff00": "#66cc33"})`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium.
    """
    try:
        # Validation runs in the mutate builder, BEFORE apply_edit opens an op record — an invalid
        # colour key/value raises here and no snapshot/record/write is ever created (E10-02).
        mutate = apply_palette_mutate(mapping, scope_ids)
        result = apply_edit(
            doc_id,
            "apply_palette",
            {"mapping": mapping, "scope_ids": scope_ids},
            mutate,
        )
    except _MAPPED as exc:
        raise _map_failure(exc) from exc

    log_tool_call(_logger, tool="apply_palette", doc_id=doc_id, operation_id=result.operation_id)
    return result
