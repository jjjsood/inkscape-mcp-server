"""Static tool metadata (tag, MCP annotations) + flag-driven disclosure.

Every registered tool has one `TOOL_META` row, stamped on in a single post-registration pass
(`apply_tool_meta`): its tag, its explicit MCP `ToolAnnotations` + title, and its compacted input
schema. The two operator flags then decide which tags stay in `tools/list`:

- `live_enabled` off        -> hide the `live` group (`live`, `live_edit`).
- `raw_action_enabled` off  -> hide the `advanced` group (`actions`).

Gating only ever NARROWS the surface: it appends `disable(...)` visibility transforms and never an
`enable`. The pass removes its own earlier transforms before re-applying, so re-running
`register_tools()` under another flag configuration (as the tests do) yields exactly the surface the
current flags allow. Path ops live inside `edit` and are gated in the op kernel, not here.

Annotation hints follow the MCP spec (2025-11-25): `readOnlyHint` for tools that never mutate,
`destructiveHint` for tools that can replace or remove existing content (every edit stays
reversible through snapshot + Operation Record), `openWorldHint` for tools that reach outside the
workspace (a running Inkscape, the host's Actions). Tags and titles are static labels; no host path
ever enters either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import mcp.types as mcp_types

from inkscape_mcp.schema_compact import compact_schema

if TYPE_CHECKING:
    from fastmcp import FastMCP

#: (tag, title, readOnly, destructive, idempotent, openWorld) per tool. `core` tools are always
#: visible. The surface tests check that every registered tool has a row.
TOOL_META: dict[str, tuple[str, str, bool, bool, bool, bool]] = {
    "open": ("core", "Open or create document", False, False, False, False),
    "inspect": ("core", "Inspect document", True, False, True, False),
    "edit": ("core", "Edit document", False, True, False, False),
    "render": ("core", "Render preview", True, False, True, False),
    "export": ("core", "Export", False, True, False, False),
    "history": ("core", "Snapshots and reload", False, True, False, False),
    "help": ("core", "Help and diagnostics", True, False, True, False),
    "live": ("live", "Live Inkscape session", False, False, False, True),
    "live_edit": ("live", "Live edit", False, True, False, True),
    "actions": ("advanced", "Inkscape Actions (advanced)", False, True, False, True),
}

#: Marker on every visibility transform this module appends, so a re-run drops only its own.
_GATING_MARKER = "_gating"


def apply_tool_meta(app: FastMCP) -> None:
    """Stamp every registered tool on `app` with its tag, annotations, title and compact schema.

    Walks the local provider's sync component registry, so it also works when an event loop is
    already running (`scripts/gen_llms_txt.py` calls `register_tools()` inside `asyncio.run`). An
    unmapped tool gets the `core` tag and the cautious default annotations. Idempotent.
    """
    from fastmcp.tools import Tool

    for component in app._local_provider._components.values():
        if isinstance(component, Tool):
            tag, title, read_only, destructive, idempotent, open_world = TOOL_META.get(
                component.name,
                ("core", component.name.replace("_", " ").capitalize(), False, True, False, True),
            )
            component.tags = {tag}
            component.title = title
            component.annotations = mcp_types.ToolAnnotations(
                title=title,
                readOnlyHint=read_only,
                destructiveHint=destructive,
                idempotentHint=idempotent,
                openWorldHint=open_world,
            )
            compact_schema(component.parameters)


def _clear_gating_transforms(app: FastMCP) -> None:
    app._transforms = [t for t in app._transforms if not getattr(t, _GATING_MARKER, False)]


def _disable(app: FastMCP, *, tags: set[str]) -> None:
    app.disable(tags=tags)
    setattr(app._transforms[-1], _GATING_MARKER, True)


def hidden_tags() -> set[str]:
    """Tags the current `live_enabled` / `raw_action_enabled` settings hide from `tools/list`."""
    from inkscape_mcp.config import get_settings

    settings = get_settings()
    off = set()
    if not settings.live_enabled:
        off.add("live")
    if not settings.raw_action_enabled:
        off.add("advanced")
    return off


def hidden_tools() -> set[str]:
    """Tool names the current flags hide (see `hidden_tags`)."""
    off = hidden_tags()
    return {name for name, (tag, *_) in TOOL_META.items() if tag in off}


def apply_disclosure(app: FastMCP) -> None:
    """NARROW `tools/list` per the current `live_enabled` / `raw_action_enabled` settings."""
    _clear_gating_transforms(app)
    for tag in sorted(hidden_tags()):
        _disable(app, tags={tag})
