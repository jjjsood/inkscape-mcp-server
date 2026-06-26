"""Intent → tool discoverability map (E14-08).

A curated, dependency-free mapping from natural-language *goals* to the concrete MCP tool name(s)
that achieve them, plus a deterministic keyword matcher and an out-of-scope detector. Pure module —
no MCP decorators, no LLM, no fuzzy lib — so it can be imported both by the `how_do_i` tool
(`inkscape_mcp.tools.discover`) and by the runtime capability probe (`inkscape_mcp.runtime.probe`,
which surfaces the same map in its `intents` section) without an import cycle.

Design (ADR-002/003):

* `how_do_i` and the `intents` capability section only RETURN guidance (tool names + a one-line
  how-to). They execute nothing. There is no raw-action escape hatch and no portmanteau tool.
* The matcher is a simple lowercase keyword/substring scorer over `INTENT_MAP`; ties and ordering
  are deterministic. No external dependency.
* Out-of-scope goals (raster/pixel editing, arbitrary Actions/extensions/scripts, network fetch,
  code execution) are detected FIRST and returned as an explicit out-of-scope result that names WHY,
  rather than being mis-mapped to a tool.

Every tool name referenced by `INTENT_MAP` must be a REAL registered tool. A test
(`tests/test_intent_discovery.py`) asserts this against the live `mcp.list_tools()` surface so the
map cannot silently drift from the tool surface.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class IntentEntry(BaseModel):
    """One curated goal → tool(s) mapping with the keywords that select it.

    `keywords` are matched (lowercased substring) against the user's goal to score this entry; they
    are NOT shown as guidance. `tools` are real registered tool names; `how_to` is a one-line
    instruction; `group` names the broad surface area (create/inspect/edit/paths/export/...).
    """

    goal_pattern: str = Field(
        description="Human-readable description of the goal this entry serves."
    )
    tools: list[str] = Field(description="Real registered MCP tool name(s) that achieve the goal.")
    how_to: str = Field(description="One-line how-to: which tool to call and the gist of how.")
    group: str = Field(description="Broad surface group, e.g. 'create', 'inspect', 'export'.")
    keywords: list[str] = Field(
        description="Lowercase keywords/phrases scored against the goal (matching only, not shown)."
    )


class IntentMatch(BaseModel):
    """A scored guidance hit returned by `how_do_i` (subset of an `IntentEntry`, no keywords)."""

    goal_pattern: str = Field(description="What this matched entry is for.")
    tools: list[str] = Field(description="Real registered tool name(s) to use.")
    how_to: str = Field(description="One-line how-to for the matched tool(s).")
    group: str = Field(description="Broad surface group of the matched tool(s).")


class OutOfScopeRule(BaseModel):
    """A curated out-of-scope category: keywords that flag it + the reason it is unsupported."""

    label: str = Field(description="Short name of the out-of-scope category.")
    reason: str = Field(description="Why it is out of scope (names the relevant boundary/ADR).")
    keywords: list[str] = Field(description="Lowercase keywords/phrases that flag this category.")


#: Curated out-of-scope categories. Detected BEFORE the in-scope matcher so a goal like "edit my
#: jpeg" returns an explicit out-of-scope result naming the boundary rather than a wrong tool.
OUT_OF_SCOPE_RULES: tuple[OutOfScopeRule, ...] = (
    OutOfScopeRule(
        label="raster/photo pixel editing",
        reason=(
            "This is a vector/SVG server — it cannot edit raster/photo PIXELS (JPEG/PNG/GIF/"
            "bitmap). It can embed or trace a raster, and it exports SVG to PNG, but pixel editing "
            "(retouch/filter/crop photo content) is out of scope."
        ),
        keywords=[
            "jpeg",
            "jpg",
            "photo",
            "photograph",
            "raster",
            "bitmap",
            "pixel",
            "retouch",
            "photoshop",
            "edit a png",
            "edit png",
            "edit the png",
            "edit my png",
        ],
    ),
    OutOfScopeRule(
        label="arbitrary Actions / extensions / scripts",
        reason=(
            "Per ADR-003 there is no raw-action/extension/script escape hatch in the MVP surface. "
            "Use the small typed tools instead; raw Inkscape Actions and arbitrary "
            "extension/script execution are not exposed."
        ),
        keywords=[
            "arbitrary action",
            "arbitrary inkscape action",
            "raw action",
            "run an action",
            "run actions",
            "run an extension",
            "run extension",
            "arbitrary extension",
            "run a script",
            "run script",
            "run a macro",
            "macro",
            "plugin",
        ],
    ),
    OutOfScopeRule(
        label="network / URL fetch",
        reason=(
            "The server is offline by policy (security sec.12: no network). It cannot fetch an "
            "image, font, or any asset from a URL or the internet. Provide local files in the "
            "workspace instead."
        ),
        keywords=[
            "download",
            "from a url",
            "from url",
            "from the internet",
            "fetch an image",
            "fetch image",
            "fetch a font",
            "fetch font",
            "http://",
            "https://",
            "from the web",
            "from the network",
            "online",
        ],
    ),
    OutOfScopeRule(
        label="code execution",
        reason=(
            "Executing arbitrary code/shell/programs is a restricted risk class and is not exposed "
            "(security sec.12: no arbitrary execution). The server only runs its own typed tools."
        ),
        keywords=[
            "execute code",
            "run code",
            "run arbitrary code",
            "run a program",
            "run python",
            "shell command",
            "run shell",
            "eval",
            "subprocess",
        ],
    ),
)


#: The curated intent map: COMMON goals → the right entry tool(s). It does not list every tool;
#: it routes the common natural-language goals to the correct entry point. Every name here must be a
#: real registered tool (guarded by `tests/test_intent_discovery.py`).
INTENT_MAP: tuple[IntentEntry, ...] = (
    # --- create / draw ---
    IntentEntry(
        goal_pattern="Draw a rectangle / square",
        tools=["create_rect"],
        how_to="Call create_rect with x, y, width, height (and rx/ry for rounded corners).",
        group="create",
        keywords=["rectangle", "rect", "square", "box", "draw a rect", "draw a box"],
    ),
    IntentEntry(
        goal_pattern="Draw a circle",
        tools=["create_circle"],
        how_to="Call create_circle with cx, cy, r.",
        group="create",
        keywords=["circle", "draw a circle", "round dot", "disc"],
    ),
    IntentEntry(
        goal_pattern="Draw an ellipse / oval",
        tools=["create_ellipse"],
        how_to="Call create_ellipse with cx, cy, rx, ry.",
        group="create",
        keywords=["ellipse", "oval", "draw an ellipse"],
    ),
    IntentEntry(
        goal_pattern="Draw a line",
        tools=["create_line"],
        how_to="Call create_line with x1, y1, x2, y2.",
        group="create",
        keywords=["line", "draw a line", "segment", "straight line"],
    ),
    IntentEntry(
        goal_pattern="Draw a polygon or polyline",
        tools=["create_polygon", "create_polyline"],
        how_to="Call create_polygon (closed) or create_polyline (open) with a list of points.",
        group="create",
        keywords=["polygon", "polyline", "many sided", "triangle", "points shape"],
    ),
    IntentEntry(
        goal_pattern="Draw an arbitrary path / freeform shape",
        tools=["create_path"],
        how_to="Call create_path with an SVG path `d` string.",
        group="create",
        keywords=["path", "freeform", "bezier", "curve", "draw a path", "path data", "d string"],
    ),
    IntentEntry(
        goal_pattern="Add text / a label",
        tools=["create_text"],
        how_to="Call create_text with x, y and the text content.",
        group="create",
        keywords=["add text", "write text", "label", "caption", "type text", "place text"],
    ),
    IntentEntry(
        goal_pattern="Group objects together / make a group",
        tools=["create_group", "group_objects"],
        how_to="Use group_objects to wrap existing object ids; create_group makes an empty group.",
        group="create",
        keywords=["group", "make a group", "group objects", "combine into group", "wrap in group"],
    ),
    IntentEntry(
        goal_pattern="Move an object into another group / reparent",
        tools=["reparent_object"],
        how_to="Call reparent_object with the object id and the target parent group id.",
        group="create",
        keywords=["reparent", "move into group", "change parent", "nest object"],
    ),
    IntentEntry(
        goal_pattern="Reuse / clone an element via <use>",
        tools=["create_use"],
        how_to="Call create_use referencing an existing element id to instance it.",
        group="create",
        keywords=["use element", "clone", "instance", "reuse element", "symbol reference"],
    ),
    IntentEntry(
        goal_pattern="Add a gradient fill (linear or radial)",
        tools=["add_linear_gradient", "add_radial_gradient"],
        how_to="Define one with add_linear_gradient / add_radial_gradient, then set_fill to it.",
        group="create",
        keywords=["gradient", "linear gradient", "radial gradient", "color fade", "blend colors"],
    ),
    IntentEntry(
        goal_pattern="Create a brand-new blank document",
        tools=["create_document"],
        how_to="Call create_document with width/height/units to start a new SVG in the workspace.",
        group="create",
        keywords=[
            "new document",
            "blank document",
            "blank svg",
            "new svg",
            "start a document",
            "start a new",
            "create canvas",
        ],
    ),
    IntentEntry(
        goal_pattern="Replace the whole document SVG or insert an SVG fragment",
        tools=["set_document_svg", "insert_svg_fragment"],
        how_to="Use set_document_svg to replace the doc; insert_svg_fragment to paste a snippet.",
        group="create",
        keywords=["set svg", "replace svg", "insert svg", "paste svg", "svg fragment", "raw svg"],
    ),
    # --- inspect / find ---
    IntentEntry(
        goal_pattern="Open an existing SVG file to work on it",
        tools=["open_document"],
        how_to="Call open_document with the workspace path to get a doc_id for the other tools.",
        group="inspect",
        keywords=["open", "load file", "open svg", "load svg", "open a document", "load document"],
    ),
    IntentEntry(
        goal_pattern="Understand a document's structure (tree, layers, styles, fonts, assets)",
        tools=["inspect_document"],
        how_to="Call inspect_document with the doc_id for the full structural picture and ids.",
        group="inspect",
        keywords=[
            "inspect",
            "structure",
            "what is in",
            "layers",
            "tree",
            "outline document",
            "overview",
        ],
    ),
    IntentEntry(
        goal_pattern="Find specific objects by color / tag / text / position",
        tools=["find_objects"],
        how_to="Call find_objects with filters (fill/stroke/tag/text/id_prefix/bbox) for ids.",
        group="inspect",
        keywords=[
            "find",
            "find objects",
            "locate",
            "search for",
            "red shapes",
            "blue shapes",
            "which objects",
            "select by",
            "get ids",
        ],
    ),
    IntentEntry(
        goal_pattern="Validate that a document is well-formed / correct",
        tools=["validate_document"],
        how_to="Call validate_document with the doc_id to check well-formedness and report issues.",
        group="inspect",
        keywords=["validate", "is it valid", "check document", "well formed", "lint", "verify svg"],
    ),
    IntentEntry(
        goal_pattern="Get quality metrics for a document",
        tools=["quality_report"],
        how_to="Call quality_report with the doc_id for machine-readable quality metrics.",
        group="inspect",
        keywords=["quality", "metrics", "quality report", "health", "score document"],
    ),
    IntentEntry(
        goal_pattern="See what the runtime / server can do (capabilities)",
        tools=["list_capabilities"],
        how_to="Call list_capabilities for the runtime matrix (incl. this intents map).",
        group="inspect",
        keywords=[
            "capabilities",
            "what can you do",
            "supported",
            "what tools",
            "features",
            "list capabilities",
        ],
    ),
    # --- edit / style ---
    IntentEntry(
        goal_pattern="Change an object's fill color",
        tools=["set_fill"],
        how_to="Call set_fill with the object id and a paint value (find_objects gets the id).",
        group="edit",
        keywords=[
            "fill",
            "fill color",
            "color it",
            "paint",
            "set color",
            "change color",
            "recolor object",
        ],
    ),
    IntentEntry(
        goal_pattern="Change an object's stroke / outline",
        tools=["set_stroke"],
        how_to="Call set_stroke with the object id and stroke paint/width.",
        group="edit",
        keywords=["stroke", "outline color", "border", "stroke width", "line color", "edge"],
    ),
    IntentEntry(
        goal_pattern="Change opacity / transparency",
        tools=["set_opacity"],
        how_to="Call set_opacity with the object id and a 0..1 opacity.",
        group="edit",
        keywords=["opacity", "transparent", "transparency", "fade", "alpha", "see through"],
    ),
    IntentEntry(
        goal_pattern="Change the font of text",
        tools=["set_font"],
        how_to="Call set_font with the text object id and font-family/size.",
        group="edit",
        keywords=["font", "typeface", "font family", "font size", "change font"],
    ),
    IntentEntry(
        goal_pattern="Replace text content",
        tools=["replace_text"],
        how_to="Call replace_text with the text object id and the new string.",
        group="edit",
        keywords=[
            "replace text",
            "replace the text",
            "change text",
            "edit text",
            "rename label",
            "new text",
            "update text",
            "text content",
        ],
    ),
    IntentEntry(
        goal_pattern="Replace one color with another across the document",
        tools=["replace_color"],
        how_to="Call replace_color with the from/to paint values to swap a color document-wide.",
        group="edit",
        keywords=[
            "replace color",
            "swap color",
            "swap all",
            "recolor document",
            "change all red",
            "color swap",
        ],
    ),
    IntentEntry(
        goal_pattern="Apply a color palette / theme to the document",
        tools=["apply_palette"],
        how_to="Call apply_palette with a palette mapping to recolor the document to a theme.",
        group="edit",
        keywords=["palette", "theme", "color scheme", "apply palette", "rebrand colors", "retheme"],
    ),
    IntentEntry(
        goal_pattern="Move / scale / rotate an object",
        tools=["move_object", "scale_object", "rotate_object"],
        how_to="Use move_object, scale_object, or rotate_object with the id and the transform.",
        group="edit",
        keywords=[
            "move",
            "scale",
            "resize object",
            "rotate",
            "transform",
            "reposition",
            "shrink",
            "enlarge",
        ],
    ),
    IntentEntry(
        goal_pattern="Duplicate or rename an object",
        tools=["duplicate_object", "rename_object"],
        how_to="Use duplicate_object to copy an object; rename_object to change its id.",
        group="edit",
        keywords=["duplicate", "copy object", "rename object", "change id", "clone object"],
    ),
    IntentEntry(
        goal_pattern="Apply several typed edits at once (batch) in one atomic operation",
        tools=["apply_edits"],
        how_to="Call apply_edits with an ordered list of typed `op` edits (one snapshot, atomic).",
        group="edit",
        keywords=[
            "batch edit",
            "multiple edits",
            "several edits",
            "many edits at once",
            "apply edits",
            "atomic edits",
            "edit in one call",
            "combine edits",
        ],
    ),
    IntentEntry(
        goal_pattern="Bulk-edit every object matching a selector (recolour all blue rects, etc.)",
        tools=["transform_objects"],
        how_to=(
            "Call transform_objects with a find_objects selector + ONE typed op (set_fill / "
            "move_object / delete_object / …); dry_run first, then dry_run=false."
        ),
        group="edit",
        keywords=[
            "bulk edit",
            "bulk",
            "every object",
            "all the",
            "recolor all",
            "recolour all",
            "nudge all",
            "move all",
            "delete all",
            "select then apply",
            "all matching",
            "apply to every",
        ],
    ),
    # --- canvas / layout ---
    IntentEntry(
        goal_pattern="Resize the canvas or fit it to the content",
        tools=["resize_canvas", "fit_to_content", "normalize_viewbox"],
        how_to="resize_canvas sets size; fit_to_content crops to art; normalize_viewbox tidies.",
        group="canvas",
        keywords=[
            "resize canvas",
            "canvas size",
            "fit to content",
            "crop",
            "viewbox",
            "page size",
            "trim",
        ],
    ),
    IntentEntry(
        goal_pattern="Tile or repeat objects across the canvas",
        tools=["tile"],
        how_to="Call tile to repeat object(s) in a grid across the canvas.",
        group="canvas",
        keywords=["tile", "repeat", "grid of", "pattern of objects", "array"],
    ),
    # --- paths / geometry ---
    IntentEntry(
        goal_pattern="Simplify / clean up paths",
        tools=["simplify_path", "cleanup_paths"],
        how_to="simplify_path reduces nodes; cleanup_paths tidies data (HIGH risk, dry-run first).",
        group="paths",
        keywords=[
            "simplify",
            "reduce nodes",
            "cleanup path",
            "clean paths",
            "optimize path",
            "fewer points",
        ],
    ),
    IntentEntry(
        goal_pattern="Combine paths or do boolean union / difference",
        tools=["combine_paths", "boolean_union", "boolean_difference"],
        how_to="combine_paths merges; boolean_union/boolean_difference do shape math (HIGH risk).",
        group="paths",
        keywords=[
            "combine paths",
            "merge paths",
            "boolean",
            "union",
            "difference",
            "subtract",
            "intersect",
        ],
    ),
    IntentEntry(
        goal_pattern="Convert a stroke into a filled path / outline",
        tools=["stroke_to_path"],
        how_to="Call stroke_to_path to outline strokes into fills (HIGH risk, dry-run first).",
        group="paths",
        keywords=["stroke to path", "outline stroke", "convert stroke", "expand stroke"],
    ),
    IntentEntry(
        goal_pattern="Break a compound path apart",
        tools=["break_apart"],
        how_to="Call break_apart to split a compound path into separate subpaths (HIGH risk).",
        group="paths",
        keywords=["break apart", "split path", "separate subpaths", "ungroup path"],
    ),
    # --- export / render ---
    IntentEntry(
        goal_pattern="Export a PNG (or other raster/PDF) of the document",
        tools=["export_document", "render_preview"],
        how_to="export_document writes a PNG/PDF/etc; render_preview gives a quick preview.",
        group="export",
        keywords=[
            "export png",
            "export a png",
            "export pdf",
            "render",
            "to png",
            "save as png",
            "rasterize",
            "make a png",
        ],
    ),
    IntentEntry(
        goal_pattern="Export just one object",
        tools=["export_object"],
        how_to="Call export_object with the doc_id and object id to export that object alone.",
        group="export",
        keywords=[
            "export object",
            "export just",
            "export one shape",
            "export selection",
            "crop export",
        ],
    ),
    IntentEntry(
        goal_pattern="Export many sizes/formats at once (batch)",
        tools=["export_batch", "create_icon_set"],
        how_to="export_batch takes a list of specs; create_icon_set makes a standard icon set.",
        group="export",
        keywords=[
            "batch export",
            "export a batch",
            "batch of",
            "many sizes",
            "multiple formats",
            "icon set",
            "icons",
            "export all sizes",
        ],
    ),
    IntentEntry(
        goal_pattern="Make the SVG smaller / optimized for the web",
        tools=["svg_web_optimize"],
        how_to="Call svg_web_optimize to losslessly shrink the SVG (reversible).",
        group="export",
        keywords=[
            "smaller",
            "optimize",
            "web optimize",
            "shrink svg",
            "minify",
            "compress svg",
            "reduce size",
            "for web",
        ],
    ),
    IntentEntry(
        goal_pattern="Export with a web or print profile",
        tools=["export_web_profile", "export_print_profile"],
        how_to="export_web_profile gives web-ready output; export_print_profile prints (CMYK/DPI).",
        group="export",
        keywords=[
            "web profile",
            "print profile",
            "for print",
            "cmyk",
            "dpi",
            "print ready",
            "web ready",
        ],
    ),
    # --- save ---
    IntentEntry(
        goal_pattern="Save the document to a new file",
        tools=["save_document_as"],
        how_to="Call save_document_as with the doc_id and a new workspace path.",
        group="save",
        keywords=["save", "save as", "write file", "save document", "export svg", "save to disk"],
    ),
    # --- snapshots / undo ---
    IntentEntry(
        goal_pattern="Snapshot, undo, or restore the document state",
        tools=["create_snapshot", "restore_snapshot", "list_snapshots"],
        how_to="create_snapshot checkpoints; list_snapshots browses; restore_snapshot rolls back.",
        group="snapshots",
        keywords=[
            "snapshot",
            "undo",
            "revert",
            "restore",
            "roll back",
            "checkpoint",
            "history",
            "previous state",
        ],
    ),
    # --- live mode ---
    IntentEntry(
        goal_pattern="Connect to a running Inkscape (live mode) and work on the open canvas",
        tools=["live_connect", "live_get_scene", "live_apply_to_selection"],
        how_to="live_connect attaches; live_get_scene reads canvas; live_apply_to_selection edits.",
        group="live",
        keywords=[
            "live",
            "running inkscape",
            "open canvas",
            "live mode",
            "connect to inkscape",
            "current selection in inkscape",
        ],
    ),
)


def _score_entry(entry: IntentEntry, goal_lc: str) -> int:
    """Count this entry's keywords that appear as substrings of the lowercased goal.

    Deterministic and dependency-free: each keyword contributes 1 to the score if it is a substring
    of the goal. A longer keyword still counts as 1 hit; ordering ties are broken by map order.
    """
    return sum(1 for kw in entry.keywords if kw in goal_lc)


def detect_out_of_scope(goal: str) -> OutOfScopeRule | None:
    """Return the first matching out-of-scope rule for `goal`, or None if none apply.

    Out-of-scope is checked before the in-scope matcher so a clearly unsupported goal (raster pixel
    editing, arbitrary Actions/extensions/scripts, network fetch, code execution) returns an
    explicit reason rather than being mis-mapped to a tool (ADR-003 / security sec.12).
    """
    goal_lc = goal.lower()
    for rule in OUT_OF_SCOPE_RULES:
        if any(kw in goal_lc for kw in rule.keywords):
            return rule
    return None


def match_intents(goal: str, limit: int = 3) -> list[IntentMatch]:
    """Score `INTENT_MAP` against `goal` (lowercased keyword substring) and return the best matches.

    Returns up to `limit` entries with a non-zero score, highest score first, ties broken by map
    order (deterministic). Returns an empty list when nothing matches (the caller then suggests
    `list_capabilities` / `inspect_document`).
    """
    goal_lc = goal.lower()
    scored: list[tuple[int, int, IntentEntry]] = []
    for idx, entry in enumerate(INTENT_MAP):
        score = _score_entry(entry, goal_lc)
        if score > 0:
            scored.append((score, idx, entry))
    # Sort by score desc, then by original map index asc (stable, deterministic).
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [
        IntentMatch(
            goal_pattern=e.goal_pattern,
            tools=e.tools,
            how_to=e.how_to,
            group=e.group,
        )
        for _, _, e in scored[: max(0, limit)]
    ]


def intents_summary() -> list[IntentMatch]:
    """The full curated map as guidance entries (no keywords), for the capabilities `intents` field.

    Surfaced by `list_capabilities`/`diagnose_runtime` so an agent can browse the whole map without
    calling `how_do_i` per goal. Same data `how_do_i` matches against, minus the keywords.

    This is the single in-memory source the `inkscape://runtime/intents` resource, the capabilities
    `intents` section, and `how_do_i` all derive from, so they cannot drift.
    """
    return [
        IntentMatch(
            goal_pattern=e.goal_pattern,
            tools=e.tools,
            how_to=e.how_to,
            group=e.group,
        )
        for e in INTENT_MAP
    ]


def intents_summary_json() -> str:
    """Serialize `intents_summary()` to a JSON object string for the runtime intents resource.

    Shape: ``{"intents": [{goal_pattern, tools, how_to, group}, ...]}`` — the SAME curated map
    `list_capabilities` exposes in its `intents` section and `how_do_i` matches against, minus the
    matcher keywords. The resource and the capabilities tool share this one accessor so the
    `inkscape://runtime/intents` payload can never diverge from the rest of the discovery surface.
    """
    entries = intents_summary()
    body = ", ".join(m.model_dump_json() for m in entries)
    return f'{{"intents": [{body}]}}'
