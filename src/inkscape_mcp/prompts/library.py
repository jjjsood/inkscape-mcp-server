"""Export/recolor prompt library (architecture §4.1).

Four `@mcp.prompt` entries that orient the agent on the shipped export/recolor surface:
`prepare_web_export`, `prepare_icon_set`, `prepare_print_export`, `theme_recoloring`. A Prompt adds
ZERO authority — it issues no command and grants no capability; it merely points the agent at the
right typed tools (which keep their own risk classes and gates). The actual work flows through
`open`, `inspect`, `edit` (ops `optimize_web`, `resize_canvas`, `normalize_viewbox`,
`replace_color`, `palette`) and `export` (file targets and the web/icons/print profiles).

Each function is registered via `@mcp.prompt` against the shared app; the module import (wired in
`server.register_tools`) runs the decorators and self-registers all four prompts.
"""

from __future__ import annotations

from inkscape_mcp.server import mcp


@mcp.prompt
def prepare_web_export() -> str:
    """Orient the agent on exporting a document as web-ready assets (PNG + plain SVG)."""
    return (
        "Goal: produce web-ready assets from the open document.\n\n"
        "1. `open(path=...)` (if not already open) to get a `doc_id`.\n"
        "2. (Optional) `inspect(doc_id, include=['quality'])` to spot oversized embedded rasters, "
        "missing fonts, or viewBox issues before exporting.\n"
        "3. (Optional) `edit(doc_id, ops=[{'op': 'optimize_web'}])` to strip editor metadata, drop "
        "unused defs/ids/empty groups, and reduce coordinate precision — reversible (snapshot + "
        "Operation Record).\n"
        "4. `export(doc_id, targets=[{'kind': 'profile', 'name': 'web', 'width_px': 1024}])` for "
        "the responsive PNG(s) plus a plain SVG (`widths=[...]` or `scales=[1, 2, 3]` for several "
        "densities). For other sizes/formats add `{'kind': 'file', 'format': 'png|pdf|svg', "
        "'width_px': ...}` targets to the SAME call; `dry_run=true` previews the projected sizes "
        "first.\n\n"
        "All exports are artifact-only and never overwrite the original; returned paths are "
        "workspace-relative."
    )


@mcp.prompt
def prepare_icon_set() -> str:
    """Orient the agent on producing a multi-size square PNG icon set."""
    return (
        "Goal: produce a multi-size square PNG icon set from the open document.\n\n"
        "1. `open(path=...)` to get a `doc_id`; check it is roughly square via "
        "`inspect(doc_id)` (icons render best from a square viewBox).\n"
        "2. (Optional) `edit(doc_id, ops=[{'op': 'optimize_web'}])` to shrink the source before "
        "rasterizing.\n"
        "3. `export(doc_id, targets=[{'kind': 'profile', 'name': 'icons', 'sizes': [...]}])` — one "
        "PNG per size (defaults to 16/32/48/64/128/256). Each size must be a positive integer "
        "within the configured pixel cap; an out-of-range size is refused before any render and "
        "no partial set is written (`dry_run=true` shows the same refusal).\n\n"
        "Outputs are artifact-only, workspace-relative, and never overwrite the original."
    )


@mcp.prompt
def prepare_print_export() -> str:
    """Orient the agent on exporting a print-ready vector PDF."""
    return (
        "Goal: produce a print-ready vector PDF of the open document.\n\n"
        "1. `open(path=...)` to get a `doc_id`.\n"
        "2. (Optional) `inspect(doc_id, include=['validate', 'quality'])` to catch missing fonts "
        "or external asset references that would break a print hand-off.\n"
        "3. `edit(doc_id, ops=[{'op': 'resize_canvas', ...}, {'op': 'normalize_viewbox'}], "
        "render='inline')` if the page area needs correcting first (reversible, and you see the "
        "result).\n"
        "4. `export(doc_id, targets=[{'kind': 'profile', 'name': 'print'}])` for a page-area "
        "vector PDF.\n\n"
        "The PDF is artifact-only and workspace-relative; the original is never touched."
    )


@mcp.prompt
def theme_recoloring() -> str:
    """Orient the agent on recoloring a document to a brand/theme palette."""
    return (
        "Goal: recolor the open document to a brand/theme palette.\n\n"
        "1. `open(path=...)` to get a `doc_id`; read the current colours via "
        "`inspect(doc_id, include=['styles'])` (it lists the distinct fill/stroke colours).\n"
        "2. For a single swap, the op `{'op': 'replace_color', 'from_color': ..., 'to_color': "
        "...}`. For a whole palette, `{'op': 'palette', 'mapping': {from: to, ...}}`. Put the ops "
        "in ONE `edit(doc_id, ops=[...], render='inline')` call: one reversible operation, and "
        "you see the result.\n"
        "3. Scope a change to part of the drawing with `ids=[...]` or `select={...}` on the op "
        "when only that part should change.\n\n"
        "Colours are validated (CSS-injection punctuation is rejected) and matched across both "
        "inline styles and presentation attributes. Every recolor is medium-risk and reversible: "
        "a pre-mutation snapshot plus an Operation Record (`history` restores it)."
    )
