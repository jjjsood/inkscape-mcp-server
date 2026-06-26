"""Tag + progressive-disclosure drift guards (E17-02).

Every registered tool must carry exactly ONE domain tag and ONE risk tag from the central
`tool_tags` map. The existing config flags (`live_enabled` / `raw_action_enabled`) then drive
tag-based EXCLUSION:

- live off  → `tools/list` omits every `live`-tagged tool,
- advanced off (`raw_action_enabled` false) → omits the ADR-003 hatch group (`run_raw_action` +
  every `paths`- and `actions`-tagged tool),
- with both ON the FULL surface returns,
- gating can ONLY narrow: an off-flag surface is a strict subset of the on-flag surface,
- `list_capabilities.tool_count` equals the ACTIVE (post-filter) surface.

The suite default (conftest) forces both flags ON, so these tests restore that state at the end.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from inkscape_mcp.config import (
    ENV_LIVE_ENABLED,
    ENV_RAW_ACTION_ENABLED,
    get_settings,
)
from inkscape_mcp.server import mcp, register_tools
from inkscape_mcp.tool_tags import DOMAIN_TAGS, RISK_TAGS

# Register the full surface once (conftest forces both flags ON, so this is the full catalog).
register_tools()


def _live_tool_names() -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def _registry_tools() -> list:
    """Every Tool component in the sync registry (the SUPERSET, pre-visibility-filter)."""
    from fastmcp.tools import Tool

    return [c for c in mcp._local_provider._components.values() if isinstance(c, Tool)]


def _set_flags(*, live: bool, advanced: bool) -> None:
    import os

    os.environ[ENV_LIVE_ENABLED] = "1" if live else "0"
    os.environ[ENV_RAW_ACTION_ENABLED] = "1" if advanced else "0"
    get_settings.cache_clear()
    register_tools()


@pytest.fixture
def restore_full_surface() -> Iterator[None]:
    """Run the body, then restore the suite-default FULL surface (both flags ON)."""
    try:
        yield
    finally:
        _set_flags(live=True, advanced=True)


# --- tagging -----------------------------------------------------------------


def test_every_tool_has_one_domain_and_one_risk_tag() -> None:
    """Each registered tool carries EXACTLY one domain tag and one risk tag — nothing else."""
    tools = _registry_tools()
    assert tools, "expected a non-empty tool surface"
    for tool in tools:
        tags = set(tool.tags)
        domain = tags & DOMAIN_TAGS
        risk = tags & RISK_TAGS
        assert len(domain) == 1, f"{tool.name}: expected one domain tag, got {domain}"
        assert len(risk) == 1, f"{tool.name}: expected one risk tag, got {risk}"
        assert tags == domain | risk, f"{tool.name}: unexpected extra tags {tags - domain - risk}"


def test_risk_tag_matches_parsed_risk_class() -> None:
    """The risk tag equals the canonical `Risk class:` token parsed from the docstring."""
    from inkscape_mcp.tools.system import _risk_class

    for tool in _registry_tools():
        risk = _risk_class(tool.description)
        expected = risk if risk in RISK_TAGS else "unknown"
        assert expected in tool.tags, f"{tool.name}: risk tag {expected!r} not in {tool.tags}"


# --- progressive disclosure --------------------------------------------------


def test_full_surface_with_both_flags_on(restore_full_surface: None) -> None:
    """With live + advanced ON, the active surface equals the full registered superset."""
    _set_flags(live=True, advanced=True)
    active = _live_tool_names()
    superset = {t.name for t in _registry_tools()}
    assert active == superset
    # The hatch + live tools are all present.
    assert "run_raw_action" in active
    assert "simplify_path" in active  # paths
    assert "list_actions" in active  # actions
    assert any(n.startswith("live_") for n in active)


def test_live_off_excludes_live_tools(restore_full_surface: None) -> None:
    """live off (advanced still on) hides EXACTLY the live-tagged tools, nothing else."""
    _set_flags(live=True, advanced=True)
    full = _live_tool_names()
    _set_flags(live=False, advanced=True)
    active = _live_tool_names()

    live_tools = {t.name for t in _registry_tools() if "live" in t.tags}
    assert live_tools, "expected a non-empty live-tagged set"
    assert active == full - live_tools
    assert not any("live" in t.tags for t in asyncio.run(mcp.list_tools()))


def test_advanced_off_excludes_hatch_tools(restore_full_surface: None) -> None:
    """advanced off (live on) hides the ADR-003 hatch group: run_raw_action + paths + actions."""
    _set_flags(live=True, advanced=True)
    full = _live_tool_names()
    _set_flags(live=True, advanced=False)
    active = _live_tool_names()

    hatch = {
        t.name
        for t in _registry_tools()
        if ("paths" in t.tags or "actions" in t.tags or t.name == "run_raw_action")
    }
    assert "run_raw_action" in hatch
    assert active == full - hatch
    assert "run_raw_action" not in active
    assert not any("paths" in t.tags or "actions" in t.tags for t in asyncio.run(mcp.list_tools()))


def test_both_off_is_strict_subset(restore_full_surface: None) -> None:
    """Gating can ONLY narrow: both-off surface is a strict subset of the both-on surface."""
    _set_flags(live=True, advanced=True)
    full = _live_tool_names()
    _set_flags(live=False, advanced=False)
    narrow = _live_tool_names()
    assert narrow < full, "gating must narrow the surface"
    # No tool the flags hide can appear in the narrowed surface (the invariant).
    hidden = {
        t.name
        for t in _registry_tools()
        if any(g in t.tags for g in ("live", "paths", "actions")) or t.name == "run_raw_action"
    }
    assert hidden, "expected a non-empty hidden set"
    assert not (narrow & hidden), f"gating leaked hidden tools: {narrow & hidden}"


def test_tool_count_reflects_active_surface(restore_full_surface: None) -> None:
    """`list_capabilities.tool_count` reports the ACTIVE (post-filter) surface, both flag states."""
    from inkscape_mcp.tools.system import list_capabilities

    _set_flags(live=True, advanced=True)
    caps_full = list_capabilities()
    assert caps_full.tool_count == len(_live_tool_names())

    _set_flags(live=False, advanced=False)
    caps_narrow = list_capabilities()
    active = _live_tool_names()
    assert caps_narrow.tool_count == len(active)
    assert {t.name for t in caps_narrow.tools} == active
    assert caps_narrow.tool_count < caps_full.tool_count


def test_gating_does_not_accumulate_transforms(restore_full_surface: None) -> None:
    """Re-running register_tools with flags ON leaves NO residual gating transform (idempotent)."""
    _set_flags(live=False, advanced=False)
    assert mcp._transforms, "expected gating transforms when flags are off"
    _set_flags(live=True, advanced=True)
    # Our gating transforms are cleared; the surface is full again.
    gating = [t for t in mcp._transforms if getattr(t, "_e17_02_gating", False)]
    assert not gating, "gating transforms must be cleared when both flags are on"
