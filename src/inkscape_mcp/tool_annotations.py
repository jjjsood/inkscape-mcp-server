"""Central MCP `ToolAnnotations` map.

Single source of truth that derives every registered tool's machine-readable MCP
`ToolAnnotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`, `title`)
from ONE central map keyed off the tool's existing risk class — no per-tool hand annotation
scattered across the tool modules. MCP clients (MCP spec 2025-11-25) read these hints to reason
about read-vs-write, destructiveness, and idempotency without parsing docstring prose.

Design (ADR-002, project risk classes):

- `readOnlyHint` is derived from the explicit `READ_ONLY_TOOLS` set, NOT from
  `risk == "low"`: read-vs-write is decoupled from the risk tier, so a read-only tool that is not
  `low` (or a `low` tool that nonetheless mutates) is hinted correctly. For the current surface the
  set is exactly the `low`-risk tools, so the derivation is behaviour-preserving.
- `destructiveHint`, `idempotentHint`, `openWorldHint`, and `title` overrides live in the
  explicit sets below. They are the ONLY hand-maintained surface; everything else is derived.

The risk class is parsed with the SAME regex the capability map / `gen_llms_txt` use
(`inkscape_mcp.tools.system._risk_class`) — there is exactly one risk vocabulary in the codebase.

sec.12: titles are static human labels only. No host path, absolute path, or `/home/` ever
appears in a title or any annotation field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import mcp.types as mcp_types

if TYPE_CHECKING:
    from fastmcp import FastMCP

#: Tools whose effect REMOVES, REPLACES, or irreversibly TRANSFORMS existing document content
#: (overwrite / delete / outline). MCP `destructiveHint=true` for exactly these. Additive writes
#: (`create_*`, `group_objects`, `duplicate_object`, `add_*_gradient`, `insert_svg_fragment`, …)
#: are NOT here — they only add structure. Every mutating op is still reversible via snapshot +
#: Operation Record (ADR-004); `destructiveHint` describes the EFFECT on existing content, not
#: recoverability.
DESTRUCTIVE_TOOLS: frozenset[str] = frozenset(
    {
        # delete
        "delete_object",
        # batch: may include a destructive member (delete / replace_*), so hint destructive
        "apply_edits",
        # selector->op fan-out: may carry a delete_object op, so hint destructive
        "transform_objects",
        # overwrite / replace whole-or-part document content
        "save_document_as",
        "set_document_svg",
        "replace_color",
        "apply_palette",
        "replace_text",
        "restore_snapshot",
        "reload_document",
        # outline / path geometry ops that consume the original geometry
        "simplify_path",
        "stroke_to_path",
        "combine_paths",
        "break_apart",
        "cleanup_paths",
        "boolean_union",
        "boolean_difference",
        # arbitrary Action surface — may overwrite/delete anything
        "run_action_chain",
        "run_raw_action",
        # live: replace / arbitrary-step against the running instance
        "live_set_selected_text",
        "live_session_step",
    }
)

#: Curated allowlist of HIGH-risk tools whose effect is ADDITIVE (they only add structure: insert a
#: fragment, apply a non-consuming edit to the live selection) and are therefore deliberately NOT in
#: `DESTRUCTIVE_TOOLS`. Every `high`-risk tool must appear in EXACTLY ONE of `DESTRUCTIVE_TOOLS` or
#: this set — the drift-guard fails otherwise, forcing a conscious destructive-vs-additive
#: classification for each new high-risk tool instead of a silent `destructiveHint=false` default.
ADDITIVE_HIGH_TOOLS: frozenset[str] = frozenset(
    {
        "insert_svg_fragment",
        "live_apply_to_selection",
        "live_insert_svg",
    }
)

#: Pure re-set ops: re-applying with IDENTICAL arguments yields IDENTICAL state. MCP
#: `idempotentHint=true` for exactly these. A setter that appends or accumulates (e.g.
#: `move_object`, `rotate_object`, `tile`) is NOT idempotent and is excluded.
IDEMPOTENT_TOOLS: frozenset[str] = frozenset(
    {
        "set_fill",
        "set_stroke",
        "set_opacity",
        "set_font",
        "rename_object",
        "resize_canvas",
        "set_document_svg",
        "live_set_viewport",
        "live_disconnect",
    }
)

#: Host/runtime probes and tools that reach OUTSIDE the document into the host environment or a
#: separate running Inkscape instance (an external entity). MCP `openWorldHint=true` for exactly
#: these; all pure in-workspace DOM ops are `openWorldHint=false`. Every `live_*` tool talks to an
#: external running Inkscape over the live transport — the canonical open-world case.
_HOST_PROBE_TOOLS: frozenset[str] = frozenset(
    {
        "diagnose_runtime",
        "list_capabilities",
        "check_live_support",
        "discover_extensions",
        "list_actions",
    }
)


def _is_open_world(name: str) -> bool:
    """True for host probes and any tool that interacts with the external live Inkscape."""
    return name in _HOST_PROBE_TOOLS or name.startswith("live_")


#: Explicit source of truth for READ-ONLY-ness. `readOnlyHint` is derived from membership
#: here — NOT from `risk == "low"` — so the read-vs-write axis is decoupled from the risk tier: a
#: future read-only tool that is not `low`, or a `low` tool that nonetheless mutates, is hinted
#: correctly by editing THIS set rather than mis-inheriting from its risk class. For the CURRENT
#: surface this set is exactly the `low`-risk tools (read-only ⟺ low today), so the change is
#: behaviour-preserving; the drift-guard asserts `readOnlyHint` is unchanged per tool and
#: that this set carries no stale names. Risk class stays a separate, independent axis.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        # system / discovery probes (read-only)
        "diagnose_runtime",
        "list_capabilities",
        "check_live_support",
        "discover_extensions",
        "list_actions",
        "how_do_i",
        "stat_artifact",
        "stat_artifacts",
        "validate_action_chain",
        # inspect / find / validate / quality (read-only reads of the document)
        "inspect_document",
        "find_objects",
        "validate_document",
        "quality_report",
        "quality_report_set",
        # render / export (read the document, write only sandboxed artifacts — read-only w.r.t. doc)
        "render_preview",
        "export_document",
        "export_object",
        "export_batch",
        "export_set",
        "export_web_profile",
        "export_print_profile",
        "create_icon_set",
        "capture_frame",
        "list_frames",
        # snapshots: list / prune / reload are read-only w.r.t. the working document
        "list_snapshots",
        "prune_snapshots",
        "create_snapshot",
        "reload_document",
        "open_document",
        # live read surface (read the running instance, do not mutate it)
        "live_status",
        "live_diff_view",
        "live_render_view",
        "live_export_selection",
        "live_get_active_document",
        "live_get_scene",
        "live_get_selection",
        "live_inspect_selection",
        "live_wait_for_change",
        "live_set_viewport",
        "live_disconnect",
    }
)


def _is_read_only(name: str) -> bool:
    """True iff `name` is classified read-only — independent of its risk tier."""
    return name in READ_ONLY_TOOLS


#: Human-readable titles. Default is derived from the tool name (`create_rect` → "Create rect");
#: this map overrides where a nicer label reads better. STATIC labels only — never a path (sec.12).
TITLE_OVERRIDES: dict[str, str] = {
    "create_rect": "Create rectangle",
    "create_ellipse": "Create ellipse",
    "create_circle": "Create circle",
    "create_polygon": "Create polygon",
    "create_polyline": "Create polyline",
    "create_use": "Create use reference",
    "create_document": "Create document",
    "create_group": "Create group",
    "create_path": "Create path",
    "create_line": "Create line",
    "create_text": "Create text",
    "create_icon_set": "Create icon set",
    "create_snapshot": "Create snapshot",
    "add_linear_gradient": "Add linear gradient",
    "add_radial_gradient": "Add radial gradient",
    "apply_edits": "Apply edits (batch)",
    "apply_palette": "Apply palette",
    "boolean_difference": "Boolean difference",
    "boolean_union": "Boolean union",
    "break_apart": "Break apart path",
    "cleanup_paths": "Clean up paths",
    "combine_paths": "Combine paths",
    "compose_grid": "Compose grid",
    "delete_object": "Delete object",
    "diagnose_runtime": "Diagnose runtime",
    "discover_extensions": "Discover extensions",
    "duplicate_object": "Duplicate object",
    "export_batch": "Export batch",
    "export_document": "Export document",
    "export_object": "Export object",
    "export_print_profile": "Export print profile",
    "export_set": "Export set",
    "export_web_profile": "Export web profile",
    "find_objects": "Find objects",
    "fit_to_content": "Fit canvas to content",
    "group_objects": "Group objects",
    "how_do_i": "How do I",
    "insert_svg_fragment": "Insert SVG fragment",
    "inspect_document": "Inspect document",
    "list_actions": "List actions",
    "list_capabilities": "List capabilities",
    "list_frames": "List frames",
    "list_snapshots": "List snapshots",
    "move_object": "Move object",
    "normalize_viewbox": "Normalize viewBox",
    "open_document": "Open document",
    "optimize_set": "Optimize set",
    "place_document": "Place document",
    "prune_snapshots": "Prune snapshots",
    "quality_report": "Quality report",
    "quality_report_set": "Quality report (set)",
    "reload_document": "Reload document",
    "rename_object": "Rename object",
    "render_preview": "Render preview",
    "reparent_object": "Reparent object",
    "replace_color": "Replace color",
    "replace_text": "Replace text",
    "resize_canvas": "Resize canvas",
    "restore_snapshot": "Restore snapshot",
    "rotate_object": "Rotate object",
    "run_action_chain": "Run action chain",
    "run_raw_action": "Run raw action",
    "save_document_as": "Save document as",
    "scale_object": "Scale object",
    "set_document_svg": "Set document SVG",
    "set_fill": "Set fill",
    "set_font": "Set font",
    "set_opacity": "Set opacity",
    "set_stroke": "Set stroke",
    "simplify_path": "Simplify path",
    "stat_artifact": "Stat artifact",
    "stat_artifacts": "Stat artifacts",
    "stroke_to_path": "Stroke to path",
    "svg_web_optimize": "Optimize SVG for web",
    "tile": "Tile object",
    "transform_objects": "Transform objects (selector)",
    "validate_action_chain": "Validate action chain",
    "validate_document": "Validate document",
    "capture_frame": "Capture frame",
    "check_live_support": "Check live support",
    "live_apply_to_selection": "Live: apply to selection",
    "live_arm_socket": "Live: arm socket",
    "live_connect": "Live: connect",
    "live_diff_view": "Live: diff view",
    "live_disconnect": "Live: disconnect",
    "live_export_selection": "Live: export selection",
    "live_get_active_document": "Live: get active document",
    "live_get_scene": "Live: get scene",
    "live_get_selection": "Live: get selection",
    "live_insert_svg": "Live: insert SVG",
    "live_inspect_selection": "Live: inspect selection",
    "live_install_helper": "Live: install helper",
    "live_render_view": "Live: render view",
    "live_session_step": "Live: session step",
    "live_set_selected_text": "Live: set selected text",
    "live_set_viewport": "Live: set viewport",
    "live_status": "Live: status",
    "live_sync_to_workspace": "Live: sync to workspace",
    "live_wait_for_change": "Live: wait for change",
}


def title_for(name: str) -> str:
    """Human-readable title for `name` — an override, or a Title-Cased label from the name."""
    if name in TITLE_OVERRIDES:
        return TITLE_OVERRIDES[name]
    return name.replace("_", " ").capitalize()


def annotations_for(name: str, risk: str) -> mcp_types.ToolAnnotations:
    """Build the `ToolAnnotations` for one tool from its name + parsed risk class.

    `readOnlyHint` is derived from the explicit `READ_ONLY_TOOLS` set — independent of
    `risk`, which stays a separate axis (still parsed/passed for the risk vocabulary, not for the
    read-vs-write hint). The remaining hints come from the central sets above. All fields are static
    — no host path ever enters a title or hint (sec.12).
    """
    _ = risk  # risk is a separate axis; readOnlyHint no longer derives from it.
    title = title_for(name)
    return mcp_types.ToolAnnotations(
        title=title,
        readOnlyHint=_is_read_only(name),
        destructiveHint=name in DESTRUCTIVE_TOOLS,
        idempotentHint=name in IDEMPOTENT_TOOLS,
        openWorldHint=_is_open_world(name),
    )


def apply_annotations(app: FastMCP) -> None:
    """Stamp every registered tool on `app` with its derived `ToolAnnotations` + `title`.

    Called ONCE at the end of `register_tools()` as a post-registration pass over the live FastMCP
    registry, so the annotations come from the same single source of truth as the capability map
    and never need per-tool hand-editing.

    Iterates the local provider's SYNC component registry and mutates each stored `Tool` object in
    place — its `annotations` / `title` propagate straight to the wire `tools/list`
    (`to_mcp_tool()`). Using the sync registry (rather than `await app.list_tools()`) means this
    works whether or not an event loop is already running — `register_tools()` is also called from
    inside `asyncio.run` by `scripts/gen_llms_txt.py`, where a nested `asyncio.run` would raise.

    Risk is parsed with the canonical `_risk_class` regex from `tools.system` — one risk
    vocabulary, no second derivation. Imported lazily to avoid an import cycle (that module imports
    the shared app).
    """
    from fastmcp.tools import Tool

    from inkscape_mcp.tools.system import _risk_class

    for component in app._local_provider._components.values():
        if not isinstance(component, Tool):
            continue
        risk = _risk_class(component.description)
        ann = annotations_for(component.name, risk)
        component.annotations = ann
        component.title = ann.title
