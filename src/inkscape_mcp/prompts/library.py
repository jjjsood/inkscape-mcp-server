"""Export/recolor prompt library (architecture §4.1).

Four `@mcp.prompt` entries that orient the agent on the shipped export/recolor tool surface:
`prepare_web_export`, `prepare_icon_set`, `prepare_print_export`, `theme_recoloring`. A Prompt adds
ZERO authority — it issues no command and grants no capability; it merely points the agent at the
right typed tools (which keep their own risk classes and gates). The actual work flows through those
tools: the export profiles (`export_web_profile` / `create_icon_set` / `export_print_profile`), the
batch export (`export_batch`), the web optimizer (`svg_web_optimize`), and the recolor tools
(`replace_color` / `apply_palette`).

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
        "1. `open_document` (if not already open) to get a `doc_id`.\n"
        "2. (Optional) `quality_report` to spot oversized embedded rasters, missing fonts, or "
        "viewBox issues before exporting.\n"
        "3. (Optional) `svg_web_optimize` to strip editor metadata, drop unused defs/ids/empty "
        "groups, and reduce coordinate precision — reversible (snapshot + Operation Record).\n"
        "4. `export_web_profile(doc_id, width_px=...)` for one PNG raster plus a plain SVG "
        "(default width 1024). For several sizes/formats at once, use `export_batch` with a typed "
        "list of specs (dry-run first to preview sizes).\n\n"
        "All exports are artifact-only and never overwrite the original; returned paths are "
        "workspace-relative."
    )


@mcp.prompt
def prepare_icon_set() -> str:
    """Orient the agent on producing a multi-size square PNG icon set."""
    return (
        "Goal: produce a multi-size square PNG icon set from the open document.\n\n"
        "1. `open_document` to get a `doc_id`; check it is roughly square via `inspect_document` "
        "(icons render best from a square viewBox).\n"
        "2. (Optional) `svg_web_optimize` to shrink the source before rasterizing.\n"
        "3. `create_icon_set(doc_id, sizes=[...])` — one PNG per size (defaults to 16/32/48/64/"
        "128/256). Each size must be a positive integer within the configured pixel cap; an "
        "out-of-range size is refused before any render and no partial set is written.\n\n"
        "Outputs are artifact-only, workspace-relative, and never overwrite the original."
    )


@mcp.prompt
def prepare_print_export() -> str:
    """Orient the agent on exporting a print-ready vector PDF."""
    return (
        "Goal: produce a print-ready vector PDF of the open document.\n\n"
        "1. `open_document` to get a `doc_id`.\n"
        "2. (Optional) `quality_report` / `validate_document` to catch missing fonts or external "
        "asset references that would break a print hand-off.\n"
        "3. `resize_canvas` / `normalize_viewbox` if the page area needs correcting first "
        "(reversible edits).\n"
        "4. `export_print_profile(doc_id)` for a page-area vector PDF.\n\n"
        "The PDF is artifact-only and workspace-relative; the original is never touched."
    )


@mcp.prompt
def theme_recoloring() -> str:
    """Orient the agent on recoloring a document to a brand/theme palette."""
    return (
        "Goal: recolor the open document to a brand/theme palette.\n\n"
        "1. `open_document` to get a `doc_id`; read the current colours via `inspect_document` "
        "(the styles section lists distinct fill/stroke colours).\n"
        "2. For a single swap, `replace_color(doc_id, from_color, to_color)`. For a whole palette "
        "in one reversible operation, `apply_palette(doc_id, mapping={from: to, ...})`.\n"
        "3. Scope a change to specific subtrees with `scope_ids=[...]` when only part of the "
        "drawing should change.\n\n"
        "Colours are validated (CSS-injection punctuation is rejected) and matched across both "
        "inline styles and presentation attributes. Every recolor is medium-risk and reversible: "
        "a pre-mutation snapshot plus a before/after preview linked to an Operation Record."
    )
