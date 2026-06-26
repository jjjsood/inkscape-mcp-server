"""Short-form tool-description mode (E20-03).

`INKSCAPE_MCP_TOOL_DESC=short` swaps each tool's wire `tools/list` description for a DERIVED short
form (summary + risk line) to cut per-turn context cost. These tests prove:

- the derivation keeps the summary + risk line and is strictly a TRIM (no invented prose);
- the short wire description tracks the canonical full docstring (single source of truth — a
  drift-guard: short ≡ `short_description(full)` for every tool, so no second hand-kept copy);
- the `inputSchema` (param names/types) is UNCHANGED, so callers keep full argument detail;
- the env floors to `full` on a stray value;
- the saving is real (short surface is materially smaller than full).

Both progressive-disclosure flags are forced ON so the registry holds the full superset, mirroring
the other drift-guards.
"""

from __future__ import annotations

import os

from fastmcp.tools import Tool

from inkscape_mcp.config import (
    ENV_LIVE_ENABLED,
    ENV_RAW_ACTION_ENABLED,
    ENV_TOOL_DESC,
    get_settings,
)
from inkscape_mcp.server import mcp, register_tools
from inkscape_mcp.tool_descriptions import short_description

os.environ[ENV_LIVE_ENABLED] = "1"
os.environ[ENV_RAW_ACTION_ENABLED] = "1"


def _register_with_desc(mode: str | None) -> dict[str, Tool]:
    """Re-register the surface under a given `INKSCAPE_MCP_TOOL_DESC` and return {name: Tool}."""
    if mode is None:
        os.environ.pop(ENV_TOOL_DESC, None)
    else:
        os.environ[ENV_TOOL_DESC] = mode
    get_settings.cache_clear()
    register_tools()
    return {c.name: c for c in mcp._local_provider._components.values() if isinstance(c, Tool)}


# --- the pure derivation -----------------------------------------------------


def test_short_description_keeps_summary_and_risk() -> None:
    full = (
        "Do a thing to the document — the one-line summary.\n\n"
        "When to use: long prose that should be dropped.\n\n"
        "Key params: `x` is a number.\n\n"
        "Risk class: medium. Reversible via snapshot.\n"
    )
    short = short_description(full)
    assert short.startswith("Do a thing to the document — the one-line summary.")
    assert "Risk class: medium. Reversible via snapshot." in short
    assert "When to use" not in short
    assert "Key params" not in short
    assert len(short) < len(full)


def test_short_description_is_idempotent_and_safe_on_plain_text() -> None:
    plain = "Already short, no structure."
    assert short_description(plain) == plain
    assert short_description("") == ""
    # Re-shortening an already-short form is stable (no risk line duplicated).
    once = short_description("Summary line.\n\nRisk class: low. Read-only.\n")
    assert short_description(once) == once


# --- registration-time behaviour + drift-guard -------------------------------


def test_short_mode_tracks_canonical_full_description() -> None:
    """Short wire description equals `short_description(full)` for every tool — no second copy."""
    full_tools = _register_with_desc("full")
    full_desc = {name: t.description or "" for name, t in full_tools.items()}

    short_tools = _register_with_desc("short")
    assert set(short_tools) == set(full_desc), "tool set changed between desc modes"

    for name, tool in short_tools.items():
        expected = short_description(full_desc[name])
        assert (tool.description or "") == expected, (
            f"{name}: short form not derived from canonical"
        )


def test_short_mode_preserves_input_schema() -> None:
    """Shortening the prose must not touch the JSON `inputSchema` (param names/types stay)."""
    full_tools = _register_with_desc("full")
    full_schemas = {name: t.parameters for name, t in full_tools.items()}

    short_tools = _register_with_desc("short")
    for name, tool in short_tools.items():
        assert tool.parameters == full_schemas[name], (
            f"{name}: inputSchema changed under short mode"
        )


def test_short_mode_actually_saves() -> None:
    """The short surface is materially smaller than the full surface (the whole point)."""
    full_total = sum(len(t.description or "") for t in _register_with_desc("full").values())
    short_total = sum(len(t.description or "") for t in _register_with_desc("short").values())
    assert short_total < full_total // 2, "short mode should at least halve description bytes"


def test_stray_value_floors_to_full() -> None:
    """A garbage env value floors to `full`: descriptions identical to the default surface."""
    full_desc = {n: t.description or "" for n, t in _register_with_desc("full").items()}
    bogus = {n: t.description or "" for n, t in _register_with_desc("garbage-value").items()}
    assert bogus == full_desc


def test_default_is_full() -> None:
    """Unset env ⇒ full descriptions (the short trim is strictly opt-in)."""
    full_desc = {n: t.description or "" for n, t in _register_with_desc("full").items()}
    default = {n: t.description or "" for n, t in _register_with_desc(None).items()}
    assert default == full_desc
