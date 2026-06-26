"""Drift-guard tests for the central MCP `ToolAnnotations` map (E17-01).

Every registered tool must expose a correct, non-default `ToolAnnotations` set derived from ONE
central map keyed off its parsed risk class:

- `readOnlyHint == (risk == 'low')` for every tool,
- `destructiveHint=true` EXACTLY for the overwrite/delete/outline set,
- `idempotentHint=true` EXACTLY for the pure re-set ops,
- `openWorldHint=true` for host probes + live tools, false for pure DOM ops,
- no host/absolute path leaks into any title or annotation field (sec.12).
"""

from __future__ import annotations

import asyncio

from inkscape_mcp.server import mcp, register_tools
from inkscape_mcp.tool_annotations import (
    DESTRUCTIVE_TOOLS,
    IDEMPOTENT_TOOLS,
    _is_open_world,
)
from inkscape_mcp.tools.system import _risk_class

# Register the full surface once so `mcp.list_tools()` reflects every `@mcp.tool` and the
# post-registration annotation pass has run (it is wired into `register_tools()`). Idempotent.
register_tools()


def _tools() -> list:
    return asyncio.run(mcp.list_tools())


def test_every_tool_has_non_default_annotations() -> None:
    """No registered tool may be left with `annotations is None` / no title (E17-01)."""
    tools = _tools()
    assert tools, "expected a non-empty tool surface"
    for tool in tools:
        assert tool.annotations is not None, f"{tool.name} has no ToolAnnotations"
        assert tool.title, f"{tool.name} has no title"
        assert tool.annotations.title == tool.title, f"{tool.name} title/annotation mismatch"


def test_read_only_hint_tracks_risk_class() -> None:
    """`readOnlyHint` is derived purely from risk: true iff the parsed risk class is `low`."""
    for tool in _tools():
        risk = _risk_class(tool.description)
        assert tool.annotations.readOnlyHint is (risk == "low"), (
            f"{tool.name}: readOnlyHint {tool.annotations.readOnlyHint} != (risk=={risk})"
        )


def test_destructive_hint_matches_exact_set() -> None:
    """`destructiveHint=true` for EXACTLY the overwrite/delete/outline set, false otherwise."""
    names = {t.name for t in _tools()}
    # The destructive set must reference only real, registered tools (no stale entries).
    assert DESTRUCTIVE_TOOLS <= names, f"unknown destructive tools: {DESTRUCTIVE_TOOLS - names}"
    for tool in _tools():
        expected = tool.name in DESTRUCTIVE_TOOLS
        assert tool.annotations.destructiveHint is expected, (
            f"{tool.name}: destructiveHint {tool.annotations.destructiveHint} != {expected}"
        )


def test_idempotent_hint_matches_exact_set() -> None:
    """`idempotentHint=true` for EXACTLY the pure re-set ops, false otherwise."""
    names = {t.name for t in _tools()}
    assert IDEMPOTENT_TOOLS <= names, f"unknown idempotent tools: {IDEMPOTENT_TOOLS - names}"
    # Spec-named pure setters must be in the set.
    for required in ("set_fill", "set_stroke", "set_opacity", "set_font", "rename_object"):
        assert required in IDEMPOTENT_TOOLS, f"{required} must be marked idempotent"
    for tool in _tools():
        expected = tool.name in IDEMPOTENT_TOOLS
        assert tool.annotations.idempotentHint is expected, (
            f"{tool.name}: idempotentHint {tool.annotations.idempotentHint} != {expected}"
        )


def test_open_world_hint_for_host_probes_and_live() -> None:
    """`openWorldHint=true` for host probes + live tools; false for pure DOM ops."""
    for tool in _tools():
        expected = _is_open_world(tool.name)
        assert tool.annotations.openWorldHint is expected, (
            f"{tool.name}: openWorldHint {tool.annotations.openWorldHint} != {expected}"
        )
    # Spec-named host probes must be open-world; a pure DOM op must not be.
    by_name = {t.name: t for t in _tools()}
    for probe in ("diagnose_runtime", "list_capabilities", "check_live_support"):
        assert by_name[probe].annotations.openWorldHint is True
    assert by_name["create_rect"].annotations.openWorldHint is False


def test_no_host_path_leaks_in_annotations() -> None:
    """sec.12: no title or annotation field may carry an absolute path or `/home/`."""
    for tool in _tools():
        ann = tool.annotations
        fields = [tool.title, ann.title]
        for value in fields:
            assert value is not None
            assert "/home/" not in value, f"{tool.name}: host path in {value!r}"
            assert not value.startswith("/"), f"{tool.name}: absolute path in {value!r}"
