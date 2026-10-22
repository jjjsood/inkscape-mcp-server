"""Progressive disclosure: tags, flag gating, and re-registration under changed flags."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.server import mcp, register_tools
from inkscape_mcp.tool_tags import TOOL_META, apply_disclosure

CORE = {"open", "inspect", "edit", "render", "export", "history", "help"}


def _names() -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_every_tool_carries_exactly_its_static_tag() -> None:
    register_tools()
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name, tool in tools.items():
        assert tool.tags == {TOOL_META[name][0]}, name
    assert {n for n, (tag, *_) in TOOL_META.items() if tag == "core"} == CORE


def test_gating_does_not_accumulate_transforms(
    surface_with_flags: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from inkscape_mcp.config import ENV_LIVE_ENABLED, ENV_RAW_ACTION_ENABLED, get_settings

    assert surface_with_flags(live=False, raw=False) == CORE
    for _ in range(3):
        apply_disclosure(mcp)
    assert _names() == CORE
    # Flip flags on and re-apply disclosure ONLY: stale hide-transforms would keep tools hidden.
    monkeypatch.setenv(ENV_LIVE_ENABLED, "1")
    monkeypatch.setenv(ENV_RAW_ACTION_ENABLED, "1")
    get_settings.cache_clear()
    apply_disclosure(mcp)
    assert _names() == set(TOOL_META)


def test_hidden_tools_are_not_callable(surface_with_flags: Any) -> None:
    from fastmcp import Client

    assert surface_with_flags(live=False, raw=False) == CORE

    async def go(name: str) -> None:
        async with Client(mcp) as client:
            await client.call_tool(name, {"action": "check" if name == "live" else "list"})

    for hidden in ("live", "actions"):
        with pytest.raises(ToolError):
            asyncio.run(go(hidden))
