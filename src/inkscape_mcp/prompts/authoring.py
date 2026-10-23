"""Authoring/compose Prompt library (architecture §4.1).

Two `@mcp.prompt` entries that on-ramp the generative surface:

* ``compose_artwork(goal)`` — orients the agent on the open → edit(render='inline') → export loop:
  build a whole drawing in ONE `edit` call, look at the inline picture, adjust, export.
* ``restyle_artwork(goal)`` — orients the agent on the OBJECT-TARGETED restyle loop: address
  objects with ``select`` (or ``inspect``), then per-object ``fill`` / ``stroke`` ops (complementing
  the document-wide ``theme_recoloring`` prompt, not duplicating it).

Like every Prompt here a Prompt adds ZERO authority (ADR-002/006) — it issues no command and grants
no capability; it merely points the agent at the right typed, gated tools (each of which keeps its
own risk class, snapshot, and Operation Record). The actual work flows through those tools.

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
    """On-ramp the generative loop: open → edit(render='inline') → look → export.

    Orients the agent to build new artwork toward `goal` from a blank canvas with typed `edit` ops,
    look at the inline picture, validate, then export. Guidance only — it executes nothing and
    adds no authority; the work flows through the gated tools it names.
    """
    clean_goal = _clean_goal(goal)
    return (
        "You are composing NEW vector artwork in inkscape-mcp. Work toward this goal:\n"
        f"  {clean_goal}\n\n"
        "Follow the open → edit → look → export loop with the typed tools (each call is atomic, "
        "risk-classed and reversible — every edit writes a snapshot + Operation Record, so "
        "`history(doc_id, action='restore', snapshot_id=...)` rolls back at any point):\n\n"
        "1. OPEN — `open(canvas={'width': ..., 'height': ...}, ops=[...], "
        "render='inline')` starts a blank SVG, returns a `doc_id`, and can already draw the first "
        "shapes. (To work on an existing file instead, `open(path=...)`.)\n"
        "2. DRAW — ONE `edit(doc_id, ops=[...], render='inline')` call can hold up to dozens of "
        "ops: `create_rect`, `create_circle`, `create_ellipse`, `create_line`, `create_polygon`, "
        "`create_polyline`, `create_path` (arbitrary `d`), `create_text`. Give a shape an `id`, or "
        "refer to what op n created as `'$n'` in a later op of the same call.\n"
        "3. PAINT — `fill`, `stroke`, `opacity`, `font` ops, or the shape's own `fill`/`stroke` "
        "fields. For gradients, a `linear_gradient` / `radial_gradient` op (id + stops) followed "
        "by `{'op': 'fill', 'ids': [...], 'color': 'url(#<gradient id>)'}` — both in the same "
        "call.\n"
        "4. COMPOSE — organize with `create_group` / `group` (wrap existing ids) and `reparent`; "
        "instance a shape with `create_use`. Lost track of an id? `inspect(doc_id, "
        "select={'fill': ..., 'tag': ...})` resolves ids, and `inspect(doc_id, include=['tree'])` "
        "gives the whole tree; edit ops also accept `select` directly.\n"
        "5. LOOK — the `edit(..., render='inline')` result already carries a picture; "
        "`render(doc_id)` returns one on demand. Decide what to adjust and iterate steps 2-5 "
        "until it matches `goal`.\n"
        "6. VALIDATE — `inspect(doc_id, include=['validate', 'quality'])` to confirm the SVG is "
        "well-formed before handing off.\n"
        "7. EXPORT — `export(doc_id, targets=[{'kind': 'file', 'format': 'png', 'width_px': 1024}, "
        "{'kind': 'file', 'format': 'svg'}])` for the final files (an `object_id` exports one "
        "element; a `{'kind': 'profile', 'name': 'icons'}` target makes an icon set). Exports are "
        "artifact-only and never overwrite the source.\n\n"
        "Everything is reversible: if an edit goes wrong, `history(doc_id, action='list')` then "
        "`restore`. There is no free-text Action or code path — only the typed tools above."
    )


@mcp.prompt
def restyle_artwork(goal: str) -> str:
    """On-ramp the OBJECT-TARGETED restyle loop: select → fill/stroke ops → render → export.

    Orients the agent to restyle SPECIFIC objects toward `goal` — address them with `select`,
    change each one's fill/stroke/opacity in one `edit` call, look, then export. This is the
    per-object companion to the document-wide `theme_recoloring` prompt (which swaps colors across
    the WHOLE document via `replace_color` / `palette`). Guidance only; adds no authority.
    """
    clean_goal = _clean_goal(goal)
    return (
        "You are RESTYLING existing objects in an open document. Work toward this goal:\n"
        f"  {clean_goal}\n\n"
        "This is the OBJECT-TARGETED restyle loop — change specific elements you have addressed. "
        "(To recolor the WHOLE document at once, prefer the `theme_recoloring` prompt: "
        "`replace_color` for one swap, `palette` for a whole mapping.) Every edit below is "
        "medium-risk and reversible (snapshot + Operation Record; `history(action='restore')` "
        "rolls back):\n\n"
        "1. OPEN — `open(path=...)` for a `doc_id` if you don't have one.\n"
        "2. ADDRESS — `inspect(doc_id, select={...})` lists the matching objects as `[id, tag, "
        "bbox, fill]` rows; filter by fill, stroke, tag, text, id_prefix, or bbox (e.g. all red "
        "shapes, or all `text`). `inspect(doc_id, include=['tree', 'styles'])` shows the full tree "
        "and the distinct colors.\n"
        "3. RESTYLE — ONE `edit(doc_id, ops=[...], render='inline')` call. Each op takes `ids` or "
        "a `select`, so no separate lookup is needed: `fill` (a color, incl. `url(#<gradient id>)` "
        "after a `linear_gradient` / `radial_gradient` op), `stroke` (color + width), `opacity` "
        "(0..1). For text, `font` / `replace_text`.\n"
        "4. BROAD SWAP (optional) — when the same color should change everywhere, "
        "`replace_color` (one from→to) or `palette` (a mapping) restyle document-wide in one "
        "reversible op; scope either with `ids` / `select`.\n"
        "5. LOOK — the edit result carries an inline picture (`render(doc_id)` for another one); "
        "iterate steps 2-5 until the result matches `goal`.\n"
        "6. EXPORT — `export(doc_id, targets=[...])` (a `file` target with `object_id` for one "
        "element) for the final artifact. Exports never overwrite the source.\n\n"
        "Colors are validated (CSS-injection punctuation rejected) and matched across both inline "
        "styles and presentation attributes. There is no free-text Action or code path — only the "
        "typed tools above."
    )
