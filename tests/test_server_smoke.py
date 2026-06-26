"""Smoke tests: the FastMCP app builds and is named. No Inkscape required."""

import asyncio

from inkscape_mcp import __version__
from inkscape_mcp.server import main, mcp, register_tools


def test_app_constructs() -> None:
    assert mcp is not None
    assert mcp.name == "inkscape-mcp"


def test_register_tools_is_callable() -> None:
    # No-op until, but must import + run without error.
    register_tools()


def test_register_tools_registers_live_canvas_assist_prompt() -> None:
    # The `live_canvas_assist` Prompt self-registers via register_tools (§4.1).
    register_tools()
    names = {p.name for p in asyncio.run(mcp.list_prompts())}
    assert "live_canvas_assist" in names


def test_entry_point_exists() -> None:
    assert callable(main)


def test_version() -> None:
    assert __version__ == "0.0.1"
