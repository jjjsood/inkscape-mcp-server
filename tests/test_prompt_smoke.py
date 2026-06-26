"""Prompt smoke tests over the in-memory MCP client (E9-05, architecture §4.1).

The four export/recolor prompts (E5-07), ``live_canvas_assist`` (E8-05), and the two E15-04
authoring prompts (``compose_artwork`` / ``restyle_artwork``) must RESOLVE and RENDER over an actual
MCP client — not merely appear in ``list_prompts``. The stale server surfaced 0 of 5, so this proves
discoverability *and* render via ``get_prompt``, and locks the count at 7.

Prompts add ZERO authority (ADR-002/003): each must point at the typed, gated tool surface and must
never mention a raw-Action escape hatch or arbitrary-code path.

Note on arguments: four prompts are zero-arg; ``live_canvas_assist``, ``compose_artwork``, and
``restyle_artwork`` each take a required ``goal`` string, so we supply a minimal goal for those to
drive the render. All seven must resolve with no argument *error* and render non-empty guidance.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client

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

# Per-prompt arguments. Four are zero-arg; `live_canvas_assist`, `compose_artwork`, and
# `restyle_artwork` each require a `goal`.
_PROMPT_ARGS: dict[str, dict[str, str]] = {
    "prepare_web_export": {},
    "prepare_icon_set": {},
    "prepare_print_export": {},
    "theme_recoloring": {},
    "live_canvas_assist": {"goal": "smoke test"},
    "compose_artwork": {"goal": "smoke test"},
    "restyle_artwork": {"goal": "smoke test"},
}

# Substrings that would betray a raw-Action / arbitrary-code escape hatch (case-insensitive).
_FORBIDDEN = ("run_raw_action", "raw action", "run_action")


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


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_prompt_resolves_and_renders_non_empty(name: str) -> None:
    # Each prompt resolves over the client with no argument error and renders non-empty guidance.
    text = _render(name, _PROMPT_ARGS[name])
    assert text.strip(), f"prompt {name!r} rendered empty guidance"


def test_prompt_count_locked_at_seven() -> None:
    async def go() -> set[str]:
        async with Client(mcp) as client:
            prompts = await client.list_prompts()
            return {p.name for p in prompts}

    names = asyncio.run(go())
    assert len(names) == 7
    assert names == _EXPECTED


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_prompt_grants_no_raw_action_path(name: str) -> None:
    # Prompts add no authority: work flows through the typed gated tools (ADR-002/003).
    text = _render(name, _PROMPT_ARGS[name]).lower()
    for forbidden in _FORBIDDEN:
        assert forbidden not in text, f"prompt {name!r} mentions a raw-Action path: {forbidden!r}"
