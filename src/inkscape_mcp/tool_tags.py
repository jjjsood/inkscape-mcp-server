"""Static tool-tag map + flag-driven disclosure.

Every registered tool carries one tag from `TOOL_TAGS`, and the two operator flags decide which
tags stay in `tools/list`:

- `live_enabled` off        -> hide the `live` group (`live`, `live_edit`).
- `raw_action_enabled` off  -> hide the `advanced` group (`actions`).

Gating only ever NARROWS the surface: it appends `disable(...)` visibility transforms and never an
`enable`. The pass removes its own earlier transforms before re-applying, so re-running
`register_tools()` under another flag configuration (as the tests do) yields exactly the surface the
current flags allow. Path ops live inside `edit` and are gated in the op kernel, not here.

Tags are static labels only; no host path ever enters a tag.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

#: Tag per tool name. `core` tools are always visible.
TOOL_TAGS: dict[str, str] = {
    "open": "core",
    "inspect": "core",
    "edit": "core",
    "render": "core",
    "export": "core",
    "history": "core",
    "help": "core",
    "live": "live",
    "live_edit": "live",
    "actions": "advanced",
}

#: Marker on every visibility transform this module appends, so a re-run drops only its own.
_GATING_MARKER = "_gating"


def apply_tags(app: FastMCP) -> None:
    """Stamp every registered tool on `app` with its tag, in place (idempotent)."""
    from fastmcp.tools import Tool

    for component in app._local_provider._components.values():
        if isinstance(component, Tool):
            component.tags = {TOOL_TAGS.get(component.name, "core")}


def _clear_gating_transforms(app: FastMCP) -> None:
    app._transforms = [t for t in app._transforms if not getattr(t, _GATING_MARKER, False)]


def _disable(app: FastMCP, *, tags: set[str]) -> None:
    app.disable(tags=tags)
    setattr(app._transforms[-1], _GATING_MARKER, True)


def apply_disclosure(app: FastMCP) -> None:
    """NARROW `tools/list` per the current `live_enabled` / `raw_action_enabled` settings."""
    from inkscape_mcp.config import get_settings

    settings = get_settings()
    _clear_gating_transforms(app)
    if not settings.live_enabled:
        _disable(app, tags={"live"})
    if not settings.raw_action_enabled:
        _disable(app, tags={"advanced"})


def apply_tags_and_disclosure(app: FastMCP) -> None:
    """Tag every tool, then apply flag-driven disclosure: the single boot entry point."""
    apply_tags(app)
    apply_disclosure(app)
