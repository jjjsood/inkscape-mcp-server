"""Prompts over the in-memory MCP client (architecture §4.1).

Every prompt must RESOLVE and RENDER over an actual MCP client — not merely appear in
``list_prompts`` — and the registered set is locked. Prompts add ZERO authority (ADR-002/003): they
never mention a raw-Action / arbitrary-code escape hatch, and every tool, op, field and enum value
they reference must exist on the registered surface (a drift guard: renaming an op or a field
fails here instead of leaving the guidance pointing at nothing).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastmcp import Client

from inkscape_mcp.edit.op_docs import op_models
from inkscape_mcp.server import mcp, register_tools

register_tools()

# The exact set of registered prompts — drift in either direction fails the count lock.
_EXPECTED = {
    "prepare_web_export",
    "prepare_icon_set",
    "prepare_print_export",
    "theme_recoloring",
    "live_canvas_assist",
    "compose_artwork",
    "restyle_artwork",
}
_GOAL_PROMPTS = ("live_canvas_assist", "compose_artwork", "restyle_artwork")

# Substrings that would betray a raw-Action / arbitrary-code escape hatch (case-insensitive).
_FORBIDDEN = ("run_raw_action", "raw action", "run_action", "--actions", "actions(")


def _args(name: str, goal: str = "smoke test") -> dict[str, str]:
    return {"goal": goal} if name in _GOAL_PROMPTS else {}


def _render(name: str, args: dict[str, str]) -> str:
    """Resolve a prompt over the in-memory client and return its concatenated rendered text."""

    async def go() -> str:
        async with Client(mcp) as client:
            result = await client.get_prompt(name, args)
            return "".join(
                message.content.text
                for message in result.messages
                if hasattr(message.content, "text")
            )

    return asyncio.run(go())


def _words(node: Any) -> Iterator[str]:
    """Every property name and string enum/const value anywhere in a JSON schema."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties":
                yield from value
            elif key == "enum":
                yield from (v for v in value if isinstance(v, str))
            elif key == "const" and isinstance(value, str):
                yield value
            yield from _words(value)
    elif isinstance(node, list):
        for item in node:
            yield from _words(item)


@pytest.fixture(scope="module")
def surface() -> dict[str, set[str]]:
    tools = asyncio.run(mcp.list_tools())
    schemas = [t.parameters for t in tools] + [m.model_json_schema() for m in op_models().values()]
    return {
        "tools": {t.name for t in tools},
        "ops": set(op_models()),
        "prompts": {p.name for p in asyncio.run(mcp.list_prompts())},
        "vocab": set(_words(schemas)),
    }


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_prompt_resolves_and_renders_non_empty(name: str) -> None:
    text = _render(name, _args(name))
    assert text.strip(), f"prompt {name!r} rendered empty guidance"


def test_prompt_set_is_locked() -> None:
    assert {p.name for p in asyncio.run(mcp.list_prompts())} == _EXPECTED


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_prompt_grants_no_raw_action_path(name: str) -> None:
    text = _render(name, _args(name)).lower()
    for forbidden in _FORBIDDEN:
        assert forbidden not in text, f"prompt {name!r} mentions a raw-Action path: {forbidden!r}"


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_prompt_references_only_the_registered_surface(name: str, surface) -> None:
    body = _render(name, _args(name))
    known = surface["tools"] | surface["ops"] | surface["prompts"] | surface["vocab"]
    # `tool(...)` calls; `url(#id)` is the CSS paint function, not a tool.
    calls = set(re.findall(r"`(\w+)\(", body)) - {"url"}
    assert calls <= surface["tools"], f"unknown tools: {calls - surface['tools']}"
    ops = set(re.findall(r"'op': '(\w+)'", body))
    assert ops <= surface["ops"], f"unknown ops: {ops - surface['ops']}"
    # Backticked identifiers and `'key': ` / `key=` argument names: a tool, op, prompt, field or
    # enum value. `'key': 'value'` / `key='value'` values must be enum/const values too, except the
    # free-form `format` string.
    idents = set(re.findall(r"`(\w+)`", body))
    pairs = re.findall(r"(?:'(\w+)':\s*|\b(\w+)=)('\w+')?", body)
    keys = {a or b for a, b, _ in pairs}
    values = {v.strip("'") for a, b, v in pairs if v and (a or b) != "format"}
    unknown = (idents | keys | values) - known
    assert not unknown, f"prompt {name!r} references names not on the surface: {sorted(unknown)}"


@pytest.mark.parametrize("name", _GOAL_PROMPTS)
def test_goal_is_woven_in_and_blank_goal_degrades(name: str) -> None:
    assert "draw a blue logo" in _render(name, _args(name, "draw a blue\n  logo"))
    assert "(no goal provided)" in _render(name, _args(name, "   "))
