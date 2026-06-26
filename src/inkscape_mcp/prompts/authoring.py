"""Authoring/compose Prompt library (E15-04, architecture §4.1).

Two `@mcp.prompt` entries that on-ramp the E14 generative surface:

* ``compose_artwork(goal)`` — orients the agent on the create → draw → render(see inline) →
  validate → export loop using the real E14 authoring tools.
* ``restyle_artwork(goal)`` — orients the agent on the OBJECT-TARGETED restyle loop:
  ``find_objects`` to address, then per-object ``set_fill`` / ``set_stroke`` (complementing the
  document-wide ``theme_recoloring`` prompt, not duplicating it).

Like every Prompt here a Prompt adds ZERO authority (ADR-002/006) — it issues no command and grants
no capability; it merely points the agent at the right typed, gated tools (each of which keeps its
own risk class, snapshot, and Operation Record). The actual work flows through those tools.

Best-practice delta (architecture §6 survey): NONE of the four compared SVG/Inkscape repos ship MCP
Prompts at all — orienting the agent on the generative loop with a first-class Prompt is unique
here.

Each function is registered via ``@mcp.prompt`` against the shared app; the module import (wired in
``server.register_tools``) runs the decorators and self-registers both prompts.
"""

from __future__ import annotations

from inkscape_mcp.server import mcp


def _clean_goal(goal: str) -> str:
    """Collapse whitespace + bound the goal so a crafted string can't break the guidance layout.

    The Prompt grants no authority, but keeping the goal to a single bounded line preserves the
    structure of the rendered instruction.
    """
    return " ".join(goal.split())[:500] or "(no goal provided)"


@mcp.prompt
def compose_artwork(goal: str) -> str:
    """On-ramp the E14 generative loop: create → draw → render(inline) → validate → export.

    Orients the agent to build new artwork toward `goal` from a blank canvas using the typed
    authoring tools, preview the inline raster, validate, then export. Guidance only — it executes
    nothing and adds no authority; the work flows through the gated tools it names.
    """
    clean_goal = _clean_goal(goal)
    return (
        "You are composing NEW vector artwork in inkscape-mcp. Work toward this goal:\n"
        f"  {clean_goal}\n\n"
        "Follow the create → draw → render → validate → export loop with the typed tools (each is "
        "small, typed, risk-classed, and reversible — every mutation writes a snapshot + Operation "
        "Record, so you can `restore_snapshot` at any point):\n\n"
        "1. CREATE — `create_document(width, height, units)` to start a blank SVG in the "
        "workspace; it returns a `doc_id` the other tools take. (To work on an existing file "
        "instead, `open_document` and skip this step.)\n"
        "2. DRAW — add geometry with the shape tools: `create_rect`, `create_circle`, "
        "`create_ellipse`, `create_line`, `create_polygon`, `create_polyline`, `create_path` "
        "(arbitrary `d`), and `create_text` for labels. Each returns the new object's `id`.\n"
        "3. PAINT — set color with `set_fill` / `set_stroke` (+ `set_opacity`). For gradients, "
        "first define one with `add_linear_gradient` / `add_radial_gradient` (each returns a "
        "gradient id), then `set_fill(doc_id, object_id, paint='url(#<gradient_id>)')` to paint "
        "the object with it.\n"
        "4. COMPOSE — organize with `create_group` / `group_objects` (wrap existing ids) and "
        "`reparent_object`; instance a shape with `create_use`. Lost track of an id? "
        "`find_objects` resolves ids by fill/stroke/tag/text/id-prefix/bbox, and "
        "`inspect_document` gives the whole tree.\n"
        "5. RENDER — `render_preview(doc_id)` returns the rasterized image INLINE so you can SEE "
        "the current result and decide what to adjust. Iterate steps 2-5 until it matches `goal`.\n"
        "6. VALIDATE — `validate_document(doc_id)` to confirm the SVG is well-formed before "
        "handing off (and `quality_report` for metrics).\n"
        "7. EXPORT — `export_document(doc_id, ...)` for the final PNG/PDF/etc (or `export_object` "
        "for one element, `export_batch` / `create_icon_set` for many sizes). Exports are "
        "artifact-only and never overwrite the source.\n\n"
        "Everything is reversible: if an edit goes wrong, `list_snapshots` then "
        "`restore_snapshot` rolls the document back. There is no raw-Action or code path — only "
        "the typed tools above."
    )


@mcp.prompt
def restyle_artwork(goal: str) -> str:
    """On-ramp the OBJECT-TARGETED restyle loop: find → set_fill/set_stroke → render → export.

    Orients the agent to restyle SPECIFIC objects toward `goal` — address them with `find_objects`,
    change each one's fill/stroke/opacity, preview, then export. This is the per-object companion to
    the document-wide `theme_recoloring` prompt (which swaps colors across the WHOLE document via
    `replace_color` / `apply_palette`). Guidance only; adds no authority.
    """
    clean_goal = _clean_goal(goal)
    return (
        "You are RESTYLING existing objects in an open document. Work toward this goal:\n"
        f"  {clean_goal}\n\n"
        "This is the OBJECT-TARGETED restyle loop — change specific elements you have addressed. "
        "(To recolor the WHOLE document at once, prefer the `theme_recoloring` prompt: "
        "`replace_color` for one swap, `apply_palette` for a whole mapping.) Every edit below is "
        "medium-risk and reversible (snapshot + Operation Record; `restore_snapshot` to roll back):"
        "\n\n"
        "1. OPEN — `open_document` for a `doc_id` if you don't have one.\n"
        "2. ADDRESS — `find_objects(doc_id, ...)` to get the ids to restyle, filtering by fill, "
        "stroke, tag, text, id-prefix, or bbox (e.g. all red shapes, or all `<text>`). "
        "`inspect_document` shows the full tree + the distinct colors in the styles section.\n"
        "3. RESTYLE — for EACH addressed id apply the change: `set_fill` (paint, incl. "
        "`url(#<gradient_id>)` after `add_linear_gradient` / `add_radial_gradient`), `set_stroke` "
        "(stroke paint + width), `set_opacity` (0..1). For text, `set_font` / `replace_text`.\n"
        "4. BROAD SWAP (optional) — when the same color should change everywhere, `replace_color` "
        "(one from→to) or `apply_palette` (a mapping) restyle document-wide in one reversible op; "
        "scope either to subtrees with `scope_ids=[...]`.\n"
        "5. RENDER — `render_preview(doc_id)` returns the raster INLINE; inspect it and iterate "
        "steps 2-5 until the result matches `goal`.\n"
        "6. EXPORT — `export_document(doc_id, ...)` (or `export_object` for one element) for the "
        "final artifact. Exports never overwrite the source.\n\n"
        "Colors are validated (CSS-injection punctuation rejected) and matched across both inline "
        "styles and presentation attributes. There is no raw-Action or code path — only the typed "
        "tools above."
    )
