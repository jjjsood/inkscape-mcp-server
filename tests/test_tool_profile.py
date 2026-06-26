"""`core` tool-disclosure profile drift-guard (E18-03).

The opt-in `INKSCAPE_MCP_TOOL_PROFILE=core` profile NARROWS `tools/list` to a curated essential set
to cut the default per-turn model-context cost. This guard asserts the security + behaviour
invariants:

- `core` yields a STRICT SUBSET of the flag-allowed (`full`) surface — gating only narrows,
- the narrowed surface is exactly the tools whose module is in `_CORE_MODULES`,
- `core` can never expose a tool the live/advanced flags hide (subset within the flag-allowed
  surface, both flag states),
- gating is idempotent + re-evaluatable: switching back to `full` restores the full surface with no
  residual profile transform,
- `list_capabilities.tool_count` / `tools` report the ACTIVE (post-profile) surface,
- a stray `INKSCAPE_MCP_TOOL_PROFILE` value floors to `full`.

The suite default (conftest) forces live + advanced ON and leaves the profile unset (`full`); each
test restores that state.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import pytest

from inkscape_mcp.config import (
    ENV_LIVE_ENABLED,
    ENV_RAW_ACTION_ENABLED,
    ENV_TOOL_PROFILE,
    TOOL_PROFILE_CORE,
    TOOL_PROFILE_FULL,
    get_settings,
)
from inkscape_mcp.server import mcp, register_tools
from inkscape_mcp.tool_tags import _CORE_MODULES, _module_leaf


def _set(*, live: bool = True, advanced: bool = True, profile: str = TOOL_PROFILE_FULL) -> None:
    os.environ[ENV_LIVE_ENABLED] = "1" if live else "0"
    os.environ[ENV_RAW_ACTION_ENABLED] = "1" if advanced else "0"
    os.environ[ENV_TOOL_PROFILE] = profile
    get_settings.cache_clear()
    register_tools()


def _active_names() -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def _registry_core_names() -> set[str]:
    from fastmcp.tools import Tool

    return {
        c.name
        for c in mcp._local_provider._components.values()
        if isinstance(c, Tool) and _module_leaf(c) in _CORE_MODULES
    }


@pytest.fixture
def restore_default_surface() -> Iterator[None]:
    """Run the body, then restore the suite default: live + advanced ON, profile full."""
    try:
        yield
    finally:
        os.environ.pop(ENV_TOOL_PROFILE, None)
        _set(live=True, advanced=True, profile=TOOL_PROFILE_FULL)


def test_core_is_strict_subset_of_full(restore_default_surface: None) -> None:
    """`core` narrows the flag-allowed surface to a strict subset (gating only narrows)."""
    _set(live=True, advanced=True, profile=TOOL_PROFILE_FULL)
    full = _active_names()
    _set(live=True, advanced=True, profile=TOOL_PROFILE_CORE)
    core = _active_names()
    assert core < full, "core profile must narrow the surface"


def test_core_surface_is_exactly_core_modules(restore_default_surface: None) -> None:
    """The narrowed surface is exactly the tools whose defining module is in `_CORE_MODULES`."""
    _set(live=True, advanced=True, profile=TOOL_PROFILE_CORE)
    active = _active_names()
    assert active == _registry_core_names()
    # Spot-check the curated essentials are present and non-core surface is gone.
    for essential in (
        "open_document",
        "inspect_document",
        "find_objects",
        "create_rect",
        "set_fill",
        "move_object",
        "export_document",
        "create_snapshot",
    ):
        assert essential in active, f"{essential} must be in the core surface"
    for excluded in ("save_document_as", "svg_web_optimize", "quality_report", "delete_object"):
        assert excluded not in active, f"{excluded} must NOT be in the core surface"


def test_core_never_exposes_flag_hidden_tools(restore_default_surface: None) -> None:
    """`core` is a subset WITHIN the flag-allowed surface — never widens past it (sec.12)."""
    # live OFF + advanced OFF defines the flag-allowed surface; core must stay inside it.
    _set(live=False, advanced=False, profile=TOOL_PROFILE_FULL)
    flag_allowed = _active_names()
    _set(live=False, advanced=False, profile=TOOL_PROFILE_CORE)
    core = _active_names()
    assert core <= flag_allowed, "core profile leaked a flag-hidden tool"
    # No live/paths/actions tool can appear via the profile.
    assert not any(n.startswith("live_") for n in core)


def test_profile_is_idempotent_and_reevaluatable(restore_default_surface: None) -> None:
    """Switching back to `full` restores the full surface with no residual profile transform."""
    _set(live=True, advanced=True, profile=TOOL_PROFILE_CORE)
    assert mcp._transforms, "expected a gating transform under the core profile"
    _set(live=True, advanced=True, profile=TOOL_PROFILE_FULL)
    full = _active_names()
    superset = _registry_core_names()  # subset; full must be a strict superset
    assert superset < full
    gating = [t for t in mcp._transforms if getattr(t, "_e17_02_gating", False)]
    assert not gating, "no gating transform should remain with all flags widest"


def test_tool_count_reflects_core_surface(restore_default_surface: None) -> None:
    """`list_capabilities` reports the ACTIVE (post-profile) surface under `core`."""
    from inkscape_mcp.tools.system import list_capabilities

    _set(live=True, advanced=True, profile=TOOL_PROFILE_FULL)
    caps_full = list_capabilities()
    _set(live=True, advanced=True, profile=TOOL_PROFILE_CORE)
    caps_core = list_capabilities()
    active = _active_names()
    assert caps_core.tool_count == len(active)
    assert {t.name for t in caps_core.tools} == active
    assert caps_core.tool_count < caps_full.tool_count


def test_stray_profile_value_floors_to_full(restore_default_surface: None) -> None:
    """A garbage `INKSCAPE_MCP_TOOL_PROFILE` value floors to `full` (never narrows by accident)."""
    _set(live=True, advanced=True, profile="garbage")
    assert get_settings().tool_profile == TOOL_PROFILE_FULL
