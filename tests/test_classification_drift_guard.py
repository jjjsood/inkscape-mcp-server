"""Classification-set drift-guard.

The hand-maintained name-keyed classification surfaces in `tool_annotations.py` / `tool_tags.py`
can drift silently as tools are added or renamed. A new tool nobody lists quietly inherits the
wrong default (`destructiveHint=false`, generic title, residual `edit` domain). This guard:

- (a) asserts every NAME referenced in `DESTRUCTIVE_TOOLS` / `IDEMPOTENT_TOOLS` /
  `ADDITIVE_HIGH_TOOLS` / `_HOST_PROBE_TOOLS` / `READ_ONLY_TOOLS` / `TITLE_OVERRIDES` resolves to a
  registered tool (no stale entry after a rename/removal),
- (b) asserts every registered `high`-risk tool is in EXACTLY ONE of `DESTRUCTIVE_TOOLS` or the
  curated `ADDITIVE_HIGH_TOOLS` allowlist — a conscious destructive-vs-additive choice, never a
  silent `destructiveHint=false`,
- (c) asserts every registered tool's defining module leaf is an EXPLICIT `_MODULE_DOMAIN` key (no
  tool falls into the residual `edit` domain by accident — a new module is caught here),
- (d) verifies the guard goes RED for an unclassified high-risk tool (synthetic fixture).

Both flags are forced ON so the registry holds the full superset (the sync component registry is
read directly, so runtime disclosure never hides a tool from this guard).
"""

from __future__ import annotations

import os

from fastmcp.tools import Tool

from inkscape_mcp.config import ENV_LIVE_ENABLED, ENV_RAW_ACTION_ENABLED, get_settings
from inkscape_mcp.server import mcp, register_tools
from inkscape_mcp.tool_annotations import (
    _HOST_PROBE_TOOLS,
    ADDITIVE_HIGH_TOOLS,
    DESTRUCTIVE_TOOLS,
    IDEMPOTENT_TOOLS,
    READ_ONLY_TOOLS,
    TITLE_OVERRIDES,
)
from inkscape_mcp.tool_tags import _MODULE_DOMAIN, _module_leaf
from inkscape_mcp.tools.system import _risk_class

os.environ[ENV_LIVE_ENABLED] = "1"
os.environ[ENV_RAW_ACTION_ENABLED] = "1"
get_settings.cache_clear()
register_tools()


def _components() -> list[Tool]:
    """Every Tool in the sync registry — the full superset, pre-visibility-filter."""
    return [c for c in mcp._local_provider._components.values() if isinstance(c, Tool)]


def _registered_names() -> set[str]:
    return {c.name for c in _components()}


# --- (a) no stale names ------------------------------------------------------


def test_no_stale_names_in_classification_sets() -> None:
    """Every name referenced in a hand-maintained classification set is a registered tool."""
    names = _registered_names()
    for label, referenced in (
        ("DESTRUCTIVE_TOOLS", set(DESTRUCTIVE_TOOLS)),
        ("IDEMPOTENT_TOOLS", set(IDEMPOTENT_TOOLS)),
        ("ADDITIVE_HIGH_TOOLS", set(ADDITIVE_HIGH_TOOLS)),
        ("_HOST_PROBE_TOOLS", set(_HOST_PROBE_TOOLS)),
        ("READ_ONLY_TOOLS", set(READ_ONLY_TOOLS)),
        ("TITLE_OVERRIDES", set(TITLE_OVERRIDES)),
    ):
        stale = referenced - names
        assert not stale, f"{label} references unregistered tool(s): {sorted(stale)}"


def test_destructive_and_additive_high_are_disjoint() -> None:
    """A tool is destructive XOR additive — never both."""
    overlap = DESTRUCTIVE_TOOLS & ADDITIVE_HIGH_TOOLS
    assert not overlap, (
        f"tools in both DESTRUCTIVE_TOOLS and ADDITIVE_HIGH_TOOLS: {sorted(overlap)}"
    )


# --- (b) every high-risk tool consciously classified -------------------------


def _unclassified_high(
    high_names: set[str], destructive: frozenset[str], additive: frozenset[str]
) -> set[str]:
    """High-risk names absent from BOTH the destructive set and the additive-high allowlist."""
    return high_names - destructive - additive


def _high_risk_names() -> set[str]:
    return {c.name for c in _components() if _risk_class(c.description) == "high"}


def test_every_high_risk_tool_is_classified() -> None:
    """No `high`-risk tool may inherit a silent `destructiveHint=false` — it must be in one set."""
    high = _high_risk_names()
    assert high, "expected a non-empty high-risk surface"
    unclassified = _unclassified_high(high, DESTRUCTIVE_TOOLS, ADDITIVE_HIGH_TOOLS)
    assert not unclassified, (
        "high-risk tools missing from BOTH DESTRUCTIVE_TOOLS and ADDITIVE_HIGH_TOOLS "
        f"(classify each): {sorted(unclassified)}"
    )


# --- (c) every module leaf explicitly mapped ---------------------------------


def test_every_module_leaf_is_explicitly_mapped() -> None:
    """Every registered tool's defining module leaf is an explicit `_MODULE_DOMAIN` key."""
    unmapped = {_module_leaf(c) for c in _components()} - set(_MODULE_DOMAIN)
    assert not unmapped, (
        f"module leaves missing from _MODULE_DOMAIN (add an explicit domain): {sorted(unmapped)}"
    )


# --- (d) guard goes RED for an unclassified high-risk tool -------------------


def test_guard_flags_an_unclassified_high_risk_tool() -> None:
    """A synthetic high-risk tool in neither set is detected — proving the guard would fail RED."""
    fixture = "_fixture_unclassified_high_tool"
    high = _high_risk_names() | {fixture}
    unclassified = _unclassified_high(high, DESTRUCTIVE_TOOLS, ADDITIVE_HIGH_TOOLS)
    assert fixture in unclassified, "guard must flag a high-risk tool that nobody classified"
