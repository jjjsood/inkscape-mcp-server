"""Explicit MCP `ToolAnnotations` per tool.

One hand-written map, checked by the surface tests: every registered tool must have an entry.
The hints follow the MCP spec (2025-11-25): `readOnlyHint` for tools that never mutate,
`destructiveHint` for tools that can replace or remove existing content (every edit stays
reversible through snapshot + Operation Record), `openWorldHint` for tools that reach outside the
workspace (a running Inkscape, the host's Actions). Titles are static labels; no host path ever
appears in an annotation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import mcp.types as mcp_types

if TYPE_CHECKING:
    from fastmcp import FastMCP

#: (title, readOnly, destructive, idempotent, openWorld) per tool.
_ANNOTATIONS: dict[str, tuple[str, bool, bool, bool, bool]] = {
    "open": ("Open or create document", False, False, False, False),
    "inspect": ("Inspect document", True, False, True, False),
    "edit": ("Edit document", False, True, False, False),
    "render": ("Render preview", True, False, True, False),
    "export": ("Export", False, True, False, False),
    "history": ("Snapshots and reload", False, True, False, False),
    "help": ("Help and diagnostics", True, False, True, False),
    "live": ("Live Inkscape session", False, False, False, True),
    "live_edit": ("Live edit", False, True, False, True),
    "actions": ("Inkscape Actions (advanced)", False, True, False, True),
}


def annotations_for(name: str) -> mcp_types.ToolAnnotations:
    """Build the `ToolAnnotations` for tool `name`; an unmapped tool gets the cautious default."""
    title, read_only, destructive, idempotent, open_world = _ANNOTATIONS.get(
        name, (name.replace("_", " ").capitalize(), False, True, False, True)
    )
    return mcp_types.ToolAnnotations(
        title=title,
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


def apply_annotations(app: FastMCP) -> None:
    """Stamp every registered tool on `app` with its `ToolAnnotations` + `title`, in place.

    Walks the local provider's sync component registry, so it also works when an event loop is
    already running (`scripts/gen_llms_txt.py` calls `register_tools()` inside `asyncio.run`).
    """
    from fastmcp.tools import Tool

    for component in app._local_provider._components.values():
        if isinstance(component, Tool):
            ann = annotations_for(component.name)
            component.annotations = ann
            component.title = ann.title
