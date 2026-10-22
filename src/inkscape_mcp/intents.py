"""Intent → (tool, op) discoverability map.

A curated, dependency-free mapping from natural-language *goals* to the tool call(s) that achieve
them, plus a deterministic keyword matcher and an out-of-scope detector. Pure module — no MCP
decorators, no LLM, no fuzzy lib — so it can be imported both by the `help` tool
(`inkscape_mcp.tools.help_`) and by the runtime capability probe (`inkscape_mcp.runtime.probe`)
without an import cycle.

Design (ADR-002/003):

* `help(goal=...)` and the `intents` capability section only RETURN guidance: the `(tool, op)`
  pair(s) to use, a one-line how-to and an example call. They execute nothing.
* A target names a tool and, where the tool is a dispatcher, the op: for `edit` the op name
  (`edit:create_rect`), for `open` the source mode (`open:canvas`), for `inspect` the include
  section, for `export` the target kind, and for `render`, `history`, `live`, `live_edit`,
  `actions` and `help` the `target` / `action` / `topic` value. Its string form is `tool:op`.
* The matcher is a simple lowercase keyword/substring scorer over `INTENT_MAP`; ties and ordering
  are deterministic. No external dependency.
* Out-of-scope goals (raster/pixel editing, arbitrary extensions/scripts, network fetch, code
  execution) are detected FIRST and returned as an explicit out-of-scope result that names WHY,
  rather than being mis-mapped to a tool.

Every target must be a REAL tool and op, and every example call must validate against the tool's
schema. `tests/test_intents.py` asserts this against the registered tool surface so the map cannot
silently drift from it.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

_PLACEHOLDER_DOC = "<doc_id>"


class IntentTarget(BaseModel):
    """One `(tool, op)` a goal is served by; `op` is the dispatcher value (see module docstring)."""

    tool: str = Field(description="One of the registered tools.")
    op: str | None = Field(
        default=None, description="Edit op / open mode / action / section / target kind."
    )

    @property
    def key(self) -> str:
        """`tool:op`, or just `tool` when the tool has no dispatcher value here."""
        return f"{self.tool}:{self.op}" if self.op else self.tool


class IntentCall(BaseModel):
    """An example call: the tool and its arguments (`<doc_id>` stands for a real document id)."""

    tool: str
    args: dict[str, Any]


class IntentEntry(BaseModel):
    """One curated goal → (tool, op) mapping with the keywords that select it.

    `keywords` are matched (lowercased substring) against the user's goal to score this entry; they
    are NOT shown as guidance. `example` is the explicit example call; when it is None and the
    first target is an `edit` op, the op's validated example from `edit/op_docs.py` is used.
    """

    goal_pattern: str = Field(
        description="Human-readable description of the goal this entry serves."
    )
    targets: list[IntentTarget] = Field(description="The `(tool, op)` pair(s) that achieve it.")
    how_to: str = Field(description="One-line how-to: which call to make and the gist of how.")
    group: str = Field(description="Broad surface group, e.g. 'create', 'inspect', 'export'.")
    keywords: list[str] = Field(
        description="Lowercase keywords/phrases scored against the goal (matching only, not shown)."
    )
    example: IntentCall | None = Field(default=None, description="Explicit example call, if any.")


class IntentMatch(BaseModel):
    """A scored guidance hit returned by `help(goal=...)` (an `IntentEntry` without keywords)."""

    goal_pattern: str = Field(description="What this matched entry is for.")
    targets: list[IntentTarget] = Field(description="The `(tool, op)` pair(s) to use.")
    how_to: str = Field(description="One-line how-to for the matched call(s).")
    group: str = Field(description="Broad surface group of the matched call(s).")
    example: IntentCall | None = Field(default=None, description="An example call to start from.")

    @property
    def keys(self) -> list[str]:
        """The targets as `tool:op` strings."""
        return [t.key for t in self.targets]


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
        label="arbitrary extensions / scripts",
        reason=(
            "Arbitrary extensions, scripts and macros are not exposed (ADR-003). The only way to "
            "reach Inkscape beyond the typed edit ops is the advanced `actions` tool, which runs "
            "allow-listed Actions and extensions only, and only when the operator enables it "
            "(INKSCAPE_MCP_RAW_ACTION_ENABLED)."
        ),
        keywords=[
            "arbitrary extension",
            "run an extension",
            "run extension",
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


def _target(spec: str) -> IntentTarget:
    tool, _, op = spec.partition(":")
    return IntentTarget(tool=tool, op=op or None)


def _entry(
    goal_pattern: str,
    group: str,
    use: list[str],
    how_to: str,
    keywords: list[str],
    example: IntentCall | None = None,
) -> IntentEntry:
    """Build an entry from `tool[:op]` strings."""
    return IntentEntry(
        goal_pattern=goal_pattern,
        targets=[_target(u) for u in use],
        how_to=how_to,
        group=group,
        keywords=keywords,
        example=example,
    )


def _call(tool: str, **args: Any) -> IntentCall:
    return IntentCall(tool=tool, args=args)


def _doc_call(tool: str, **args: Any) -> IntentCall:
    return IntentCall(tool=tool, args={"doc_id": _PLACEHOLDER_DOC, **args})


#: The curated intent map: COMMON goals → the right `(tool, op)`. It does not list every op; it
#: routes the common natural-language goals to the correct entry point (`help(topic='ops')` lists
#: every op). Guarded by `tests/test_intents.py`.
INTENT_MAP: tuple[IntentEntry, ...] = (
    # --- create / draw (all are `edit` ops; several can share ONE call) ---
    _entry(
        "Draw a rectangle / square",
        "create",
        ["edit:create_rect"],
        "edit(ops=[{op:'create_rect', x, y, width, height}]); rx/ry round the corners.",
        ["rectangle", "rect", "square", "box", "draw a rect", "draw a box"],
    ),
    _entry(
        "Draw a circle",
        "create",
        ["edit:create_circle"],
        "edit(ops=[{op:'create_circle', cx, cy, r}]).",
        ["circle", "draw a circle", "round dot", "disc"],
    ),
    _entry(
        "Draw an ellipse / oval",
        "create",
        ["edit:create_ellipse"],
        "edit(ops=[{op:'create_ellipse', cx, cy, rx, ry}]).",
        ["ellipse", "oval", "draw an ellipse"],
    ),
    _entry(
        "Draw a line",
        "create",
        ["edit:create_line"],
        "edit(ops=[{op:'create_line', x1, y1, x2, y2, stroke}]).",
        ["line", "draw a line", "segment", "straight line"],
    ),
    _entry(
        "Draw a polygon or polyline",
        "create",
        ["edit:create_polygon", "edit:create_polyline"],
        "create_polygon (closed) or create_polyline (open) with points=[[x,y],...].",
        ["polygon", "polyline", "many sided", "triangle", "points shape"],
    ),
    _entry(
        "Draw an arbitrary path / freeform shape",
        "create",
        ["edit:create_path"],
        "edit(ops=[{op:'create_path', d:'M10 10 L90 10 Z'}]) with an SVG path `d` string.",
        ["path", "freeform", "bezier", "curve", "draw a path", "path data", "d string"],
    ),
    _entry(
        "Add text / a label",
        "create",
        ["edit:create_text"],
        "edit(ops=[{op:'create_text', x, y, text}]); style it with the `font` op.",
        ["add text", "write text", "label", "caption", "type text", "place text"],
    ),
    _entry(
        "Group objects together / make a group",
        "create",
        ["edit:group", "edit:create_group"],
        "group wraps existing ids (ids=[...]); create_group makes an empty group to fill later.",
        ["group", "make a group", "group objects", "combine into group", "wrap in group"],
    ),
    _entry(
        "Move an object into another group / reparent",
        "create",
        ["edit:reparent"],
        "edit(ops=[{op:'reparent', ids:[...], parent:'<group id>'}]).",
        ["reparent", "move into group", "change parent", "nest object"],
    ),
    _entry(
        "Reuse / clone an element via <use>",
        "create",
        ["edit:create_use"],
        "edit(ops=[{op:'create_use', href:'<element id>', x, y}]) instances an existing element.",
        ["use element", "clone", "instance", "reuse element", "symbol reference"],
    ),
    _entry(
        "Add a gradient fill (linear or radial)",
        "create",
        ["edit:linear_gradient", "edit:radial_gradient"],
        "Define it with linear_gradient/radial_gradient (id + stops), then fill with 'url(#id)'; "
        "both can be ops of the same call.",
        ["gradient", "linear gradient", "radial gradient", "color fade", "blend colors"],
    ),
    _entry(
        "Create a brand-new blank document",
        "create",
        ["open:canvas"],
        "open(canvas={width, height}, ops=[...first edits...], render='inline') returns a doc_id.",
        [
            "new document",
            "blank document",
            "blank svg",
            "new svg",
            "start a document",
            "start a new",
            "create canvas",
        ],
        _call(
            "open",
            canvas={"width": 200, "height": 100, "background": "#ffffff"},
            ops=[{"op": "create_circle", "cx": 100, "cy": 50, "r": 30, "fill": "#3366ff"}],
            render="inline",
        ),
    ),
    _entry(
        "Replace the whole document SVG or insert an SVG fragment",
        "create",
        ["edit:set_svg", "edit:insert_svg"],
        "set_svg replaces the document; insert_svg pastes a snippet "
        "(both HIGH risk: approval_token).",
        [
            "set svg",
            "replace svg",
            "insert svg",
            "paste svg",
            "svg fragment",
            "svg snippet",
            "snippet",
            "raw svg",
        ],
    ),
    _entry(
        "Compose several documents or objects into a grid / place one document in another",
        "create",
        ["edit:grid", "edit:place"],
        "grid lays out doc_ids (or source_ids of one source_doc_id) in rows x cols; place puts one "
        "document into this one at x, y.",
        ["contact sheet", "sprite sheet", "grid of documents", "place document", "compose grid"],
    ),
    # --- open / inspect / find ---
    _entry(
        "Open an existing SVG file to work on it",
        "inspect",
        ["open:path"],
        "open(path='logo.svg') returns a doc_id for all other tools; add ops to edit right away.",
        ["open", "load file", "open svg", "load svg", "open a document", "load document"],
        _call("open", path="logo.svg"),
    ),
    _entry(
        "Understand a document's structure (tree, layers, styles, fonts, assets)",
        "inspect",
        ["inspect:summary", "inspect:tree", "inspect:layers"],
        "inspect(doc_id, include=['summary','tree','layers','styles','fonts','assets']).",
        [
            "inspect",
            "structure",
            "what is in",
            "layers",
            "tree",
            "outline document",
            "overview",
        ],
        _doc_call("inspect", include=["summary", "tree"]),
    ),
    _entry(
        "Find specific objects by color / tag / text / position",
        "inspect",
        ["inspect:objects"],
        "inspect(doc_id, select={fill, stroke, tag, text, id_prefix, bbox}); edit ops accept the "
        "same `select` instead of ids.",
        [
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
        _doc_call("inspect", select={"fill": "#ff0000"}),
    ),
    _entry(
        "Validate that a document is well-formed / correct",
        "inspect",
        ["inspect:validate"],
        "inspect(doc_id, include=['validate']) checks well-formedness and reports issues.",
        ["validate", "is it valid", "check document", "well formed", "lint", "verify svg"],
        _doc_call("inspect", include=["validate"]),
    ),
    _entry(
        "Get quality metrics for a document",
        "inspect",
        ["inspect:quality"],
        "inspect(doc_id, include=['quality']) returns machine-readable quality metrics.",
        ["quality", "metrics", "quality report", "health", "score document"],
        _doc_call("inspect", include=["quality"]),
    ),
    _entry(
        "See what the runtime / server can do (capabilities)",
        "inspect",
        ["help:capabilities"],
        "help(topic='capabilities') shows the runtime matrix; help(topic='ops') lists every op.",
        [
            "capabilities",
            "what can you do",
            "supported",
            "what tools",
            "features",
            "list capabilities",
        ],
        _call("help", topic="capabilities"),
    ),
    _entry(
        "Diagnose why Inkscape or an export is not working",
        "inspect",
        ["help:diagnose"],
        "help(topic='diagnose') re-probes the runtime (Inkscape binary, versions, flags).",
        [
            "diagnose",
            "not working",
            "doesn't work",
            "does not work",
            "troubleshoot",
            "inkscape not found",
            "engine unavailable",
        ],
        _call("help", topic="diagnose"),
    ),
    _entry(
        "Look at the current result (preview image)",
        "inspect",
        ["render:page", "render:object"],
        "render(doc_id) returns an inline PNG; target='object' + object_id zooms to one object. "
        "edit(..., render='inline') already returns one.",
        [
            "preview",
            "look at",
            "show me the",
            "see the result",
            "screenshot",
            "what does it look like",
            "view the",
        ],
        _doc_call("render", width_px=800),
    ),
    # --- edit / style ---
    _entry(
        "Change an object's fill color",
        "edit",
        ["edit:fill"],
        "edit(ops=[{op:'fill', ids:[...] | select:{...}, color}]).",
        [
            "fill",
            "fill color",
            "color it",
            "paint",
            "set color",
            "change color",
            "recolor object",
        ],
    ),
    _entry(
        "Change an object's stroke / outline",
        "edit",
        ["edit:stroke"],
        "edit(ops=[{op:'stroke', ids:[...] | select:{...}, color, width}]).",
        ["stroke", "outline color", "border", "stroke width", "line color", "edge"],
    ),
    _entry(
        "Change opacity / transparency",
        "edit",
        ["edit:opacity"],
        "edit(ops=[{op:'opacity', ids:[...], opacity: 0..1}]).",
        ["opacity", "transparent", "transparency", "fade", "alpha", "see through"],
    ),
    _entry(
        "Change the font of text",
        "edit",
        ["edit:font"],
        "edit(ops=[{op:'font', select:{tag:'text'}, family, size}]); "
        "glyph gaps come back as warnings.",
        ["font", "typeface", "font family", "font size", "change font"],
    ),
    _entry(
        "Replace text content",
        "edit",
        ["edit:replace_text"],
        "edit(ops=[{op:'replace_text', ids:['<text id>'], text}]).",
        [
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
    _entry(
        "Replace one color with another across the document",
        "edit",
        ["edit:replace_color"],
        "edit(ops=[{op:'replace_color', from_color, to_color}]) swaps a color document-wide.",
        [
            "replace color",
            "swap color",
            "swap all",
            "recolor document",
            "change all red",
            "color swap",
        ],
    ),
    _entry(
        "Apply a color palette / theme to the document",
        "edit",
        ["edit:palette"],
        "edit(ops=[{op:'palette', mapping:{'#old':'#new', ...}}]) recolors to a theme.",
        ["palette", "theme", "color scheme", "apply palette", "rebrand colors", "retheme"],
    ),
    _entry(
        "Move / scale / rotate an object",
        "edit",
        ["edit:move", "edit:scale", "edit:rotate"],
        "move (dx, dy), scale (sx[, sy]) or rotate (degrees) on ids or a select.",
        [
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
    _entry(
        "Duplicate or rename an object",
        "edit",
        ["edit:duplicate", "edit:rename"],
        "duplicate copies an object (new_id); rename changes its id or label.",
        ["duplicate", "copy object", "rename object", "change id", "clone object"],
    ),
    _entry(
        "Delete an object",
        "edit",
        ["edit:delete"],
        "edit(ops=[{op:'delete', ids:[...]}], approval_token) — HIGH risk, dry_run=true shows what "
        "would go.",
        ["delete", "remove object", "remove the shape", "erase", "get rid of"],
    ),
    # --- canvas / layout ---
    _entry(
        "Resize the canvas or fit it to the content",
        "canvas",
        ["edit:resize_canvas", "edit:fit_to_content", "edit:normalize_viewbox"],
        "resize_canvas sets the size; fit_to_content crops to the art; normalize_viewbox tidies.",
        [
            "resize canvas",
            "canvas size",
            "fit to content",
            "crop",
            "viewbox",
            "page size",
            "trim",
        ],
    ),
    _entry(
        "Tile or repeat objects across the canvas",
        "canvas",
        ["edit:tile"],
        "edit(ops=[{op:'tile', ids:[...], rows, cols, dx, dy}]) repeats objects in a grid.",
        ["tile", "repeat", "grid of", "pattern of objects", "array"],
    ),
    # --- paths / geometry (HIGH risk; need raw_action_enabled) ---
    _entry(
        "Simplify / clean up paths",
        "paths",
        ["edit:simplify", "edit:cleanup"],
        "simplify reduces nodes; cleanup tidies path data (HIGH risk: dry_run first).",
        [
            "simplify",
            "reduce nodes",
            "cleanup path",
            "clean paths",
            "optimize path",
            "fewer points",
        ],
    ),
    _entry(
        "Combine paths or do boolean union / difference",
        "paths",
        ["edit:combine", "edit:union", "edit:difference"],
        "combine merges; union/difference do shape math on ids (HIGH risk).",
        [
            "combine paths",
            "merge paths",
            "boolean",
            "union",
            "difference",
            "subtract",
            "intersect",
        ],
    ),
    _entry(
        "Convert a stroke into a filled path / outline",
        "paths",
        ["edit:stroke_to_path"],
        "edit(ops=[{op:'stroke_to_path', ids:[...]}]) outlines strokes into fills (HIGH risk).",
        ["stroke to path", "outline stroke", "convert stroke", "expand stroke"],
    ),
    _entry(
        "Break a compound path apart",
        "paths",
        ["edit:break_apart"],
        "edit(ops=[{op:'break_apart', ids:[...]}]) splits a compound path (HIGH risk).",
        ["break apart", "split path", "separate subpaths", "ungroup path"],
    ),
    # --- export ---
    _entry(
        "Export a PNG (or other raster/PDF) of the document",
        "export",
        ["export:file"],
        "export(doc_id, targets=[{kind:'file', format:'png|pdf|svg', width_px}]).",
        [
            "export png",
            "export a png",
            "export pdf",
            "render",
            "to png",
            "save as png",
            "rasterize",
            "make a png",
        ],
        _doc_call("export", targets=[{"kind": "file", "format": "png", "width_px": 1024}]),
    ),
    _entry(
        "Export just one object",
        "export",
        ["export:file"],
        "export(doc_id, targets=[{kind:'file', format:'png', object_id:'<id>'}]).",
        [
            "export object",
            "export just",
            "export one shape",
            "export selection",
            "crop export",
        ],
        _doc_call("export", targets=[{"kind": "file", "format": "png", "object_id": "logo"}]),
    ),
    _entry(
        "Export many sizes/formats at once (batch / icon set)",
        "export",
        ["export:file", "export:profile"],
        "Put several targets in ONE export call; profile name='icons' makes a standard icon set.",
        [
            "batch export",
            "export a batch",
            "batch of",
            "many sizes",
            "multiple formats",
            "icon set",
            "icons",
            "export all sizes",
        ],
        _doc_call(
            "export",
            targets=[
                {"kind": "file", "format": "png", "width_px": 512},
                {"kind": "file", "format": "svg"},
                {"kind": "profile", "name": "icons"},
            ],
        ),
    ),
    _entry(
        "Make the SVG smaller / optimized for the web",
        "export",
        ["edit:optimize_web"],
        "edit(ops=[{op:'optimize_web'}]) shrinks the SVG (reversible); then export a web profile.",
        [
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
    _entry(
        "Export with a web or print profile",
        "export",
        ["export:profile"],
        "export(doc_id, targets=[{kind:'profile', name:'web|print'}]); "
        "dry_run=true projects sizes.",
        [
            "web profile",
            "print profile",
            "for print",
            "cmyk",
            "dpi",
            "print ready",
            "web ready",
        ],
        _doc_call("export", targets=[{"kind": "profile", "name": "web"}]),
    ),
    _entry(
        "Save the document to a new file",
        "save",
        ["export:save_as"],
        "export(doc_id, targets=[{kind:'save_as', dest_path:'out.svg'}]); "
        "overwrite needs approval.",
        ["save", "save as", "write file", "save document", "export svg", "save to disk"],
        _doc_call("export", targets=[{"kind": "save_as", "dest_path": "out.svg"}]),
    ),
    # --- history / undo ---
    _entry(
        "Clean up / prune old history and free snapshot space",
        "history",
        ["history:prune"],
        "history(doc_id, action='prune') deletes old snapshots/operation records and live frames "
        "beyond the retention limits and reports freed_bytes. Recent snapshots are kept.",
        [
            "prune",
            "clean up the history",
            "clean up history",
            "cleanup history",
            "tidy history",
            "tidy up the history",
            "free snapshot space",
            "free up space",
            "snapshot space",
            "reduce history",
            "shrink history",
            "old snapshots",
            "delete snapshots",
            "clear history",
            "history size",
        ],
        _doc_call("history", action="prune"),
    ),
    _entry(
        "Snapshot, undo, or restore the document state",
        "history",
        ["history:list", "history:create", "history:restore"],
        "history(doc_id, action='list|create|restore'); restore is itself reversible.",
        [
            "snapshot",
            "undo",
            "revert",
            "restore",
            "roll back",
            "checkpoint",
            "history",
            "previous state",
        ],
        _doc_call("history", action="restore", snapshot_id="snap-0003"),
    ),
    # --- live mode ---
    _entry(
        "Connect to a running Inkscape (live mode) and work on the open canvas",
        "live",
        ["live:observe", "live_edit:apply"],
        "live(action='observe', include=['selection','inspect','view']) connects automatically; "
        "live_edit(action='apply', style={...}) changes the selection.",
        [
            "live",
            "running inkscape",
            "open canvas",
            "live mode",
            "connect to inkscape",
            "current selection in inkscape",
        ],
        _call("live", action="observe", include=["selection", "inspect", "view"]),
    ),
    # --- advanced ---
    _entry(
        "Run an allow-listed Inkscape Action chain (advanced)",
        "advanced",
        ["actions:validate", "actions:run"],
        "actions(action='list') shows what is exposed; 'validate' checks steps; 'run' needs "
        "approval_token and raw_action_enabled. Prefer edit ops when one exists.",
        [
            "inkscape action",
            "action chain",
            "run an action",
            "run actions",
            "raw action",
            "arbitrary action",
            "arbitrary inkscape action",
        ],
        _call("actions", action="validate", steps=[{"action": "select-all"}]),
    ),
)


def _example_for(entry: IntentEntry) -> IntentCall | None:
    """The explicit example, else the first `edit` op's validated example from `op_docs`."""
    if entry.example is not None:
        return entry.example
    first = entry.targets[0] if entry.targets else None
    if first is None or first.tool != "edit" or first.op is None:
        return None
    from inkscape_mcp.edit.op_docs import OP_EXAMPLES  # local: op_docs pulls in the edit engine

    op = OP_EXAMPLES.get(first.op)
    return None if op is None else _doc_call("edit", ops=[op])


def _match(entry: IntentEntry) -> IntentMatch:
    return IntentMatch(
        goal_pattern=entry.goal_pattern,
        targets=entry.targets,
        how_to=entry.how_to,
        group=entry.group,
        example=_example_for(entry),
    )


#: German matcher keywords by `goal_pattern` (agents often pass the user's own words). Kept out of
#: `INTENT_MAP` so the published intents stay English; only the matcher reads them.
_GERMAN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Draw a rectangle / square": ("rechteck", "quadrat"),
    "Draw a circle": ("kreis",),
    "Add text / a label": ("text hinzu", "text anlegen", "beschriftung", "schreib"),
    "Group objects together / make a group": ("gruppier",),
    "Create a brand-new blank document": ("neue datei", "neues dokument", "leere fläche"),
    "Open an existing SVG file to work on it": ("öffne", "oeffne", "datei laden"),
    "Understand a document's structure (tree, layers, styles, fonts, assets)": (
        "struktur",
        "ebenen",
    ),
    "Validate that a document is well-formed / correct": ("validier", "prüf"),
    "Diagnose why Inkscape or an export is not working": (
        "funktioniert",
        "installation",
        "geht nicht",
    ),
    "Look at the current result (preview image)": ("zeig mir", "vorschau", "ansehen"),
    "Change an object's fill color": ("färbe", "faerbe", "farbe ändern", "füllung"),
    "Change an object's stroke / outline": ("kontur", "rand", "strichstärke"),
    "Change opacity / transparency": ("deckkraft", "transparen"),
    "Change the font of text": ("schrift",),
    "Replace text content": ("text ersetzen", "text ändern", "ersetze den text"),
    "Replace one color with another across the document": ("ersetze", "farbe tauschen", "umfärb"),
    "Apply a color palette / theme to the document": ("farbpalette", "farbschema"),
    "Move / scale / rotate an object": (
        "verschieb",
        "skalier",
        "dreh",
        "größer",
        "kleiner machen",
        "vergrößer",
        "verkleiner",
    ),
    "Duplicate or rename an object": ("dupliz", "kopier", "umbenenn"),
    "Delete an object": ("lösch", "loesch", "entfern"),
    "Resize the canvas or fit it to the content": ("zuschneiden", "seitengröße", "zeichenfläche"),
    "Export a PNG (or other raster/PDF) of the document": ("als png", "exportier"),
    "Export many sizes/formats at once (batch / icon set)": ("icon-set", "mehrere größen"),
    "Make the SVG smaller / optimized for the web": (
        "kleiner",
        "fürs web",
        "für das web",
        "optimier",
    ),
    "Save the document to a new file": ("speicher",),
    "Snapshot, undo, or restore the document state": ("rückgängig", "wiederherstell", "zurück"),
}


def _score_entry(entry: IntentEntry, goal_lc: str) -> int:
    """Count this entry's keywords that appear as substrings of the lowercased goal.

    Deterministic and dependency-free: each keyword contributes 1 to the score if it is a substring
    of the goal. A longer keyword still counts as 1 hit; ordering ties are broken by map order.
    """
    german = _GERMAN_KEYWORDS.get(entry.goal_pattern, ())
    return sum(1 for kw in (*entry.keywords, *german) if kw in goal_lc)


def detect_out_of_scope(goal: str) -> OutOfScopeRule | None:
    """Return the first matching out-of-scope rule for `goal`, or None if none apply.

    Out-of-scope is checked before the in-scope matcher so a clearly unsupported goal (raster pixel
    editing, arbitrary extensions/scripts, network fetch, code execution) returns an explicit
    reason rather than being mis-mapped to a tool (ADR-003 / security sec.12).
    """
    goal_lc = goal.lower()
    for rule in OUT_OF_SCOPE_RULES:
        if any(kw in goal_lc for kw in rule.keywords):
            return rule
    return None


def match_intents(goal: str, limit: int = 3) -> list[IntentMatch]:
    """Score `INTENT_MAP` against `goal` (lowercased keyword substring) and return the best matches.

    Returns up to `limit` entries with a non-zero score, highest score first, ties broken by map
    order (deterministic). Entries targeting a tool the current flags hide (`live`, `actions`) are
    skipped so guidance never points at an uncallable tool. Returns an empty list when nothing
    matches (the caller then suggests `help(topic='ops')`).
    """
    goal_lc = goal.lower()
    from inkscape_mcp.tool_tags import hidden_tools

    hidden = hidden_tools()
    scored: list[tuple[int, int, IntentEntry]] = []
    for idx, entry in enumerate(INTENT_MAP):
        if any(t.tool in hidden for t in entry.targets):
            continue
        score = _score_entry(entry, goal_lc)
        if score > 0:
            scored.append((score, idx, entry))
    # Sort by score desc, then by original map index asc (stable, deterministic).
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [_match(e) for _, _, e in scored[: max(0, limit)]]


def intents_summary() -> list[IntentMatch]:
    """The full curated map as guidance entries (no keywords), for the capabilities `intents` field.

    This is the single in-memory source the `inkscape://runtime/intents` resource, the capabilities
    `intents` section and `help(goal=...)` all derive from, so they cannot drift.
    """
    return [_match(e) for e in INTENT_MAP]


def intents_summary_json() -> str:
    """Serialize `intents_summary()` to a JSON object string for the runtime intents resource.

    Shape: ``{"intents": [{goal_pattern, targets, how_to, group, example}, ...]}`` — the same map
    `help(goal=...)` matches against, minus the matcher keywords.
    """
    entries = [m.model_dump(mode="json", exclude_none=True) for m in intents_summary()]
    return json.dumps({"intents": entries}, ensure_ascii=False, separators=(",", ":"))
