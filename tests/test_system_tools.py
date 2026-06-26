"""System-tool + runtime-resource registration/agreement tests."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.resources.runtime import runtime_capabilities
from inkscape_mcp.runtime.probe import Capabilities
from inkscape_mcp.server import mcp, register_tools
from inkscape_mcp.tools import system as system_mod
from inkscape_mcp.tools.system import (
    diagnose_runtime,
    list_capabilities,
    stat_artifact,
    stat_artifacts,
)

# Register the full surface once so `mcp.list_tools()` reflects every `@mcp.tool` independent of
# test ordering (asserts the authoritative count against the whole registry). Idempotent.
register_tools()


def _tool_names() -> set[str]:
    tools = asyncio.run(mcp.list_tools())
    return {t.name for t in tools}


def _resource_uris() -> set[str]:
    resources = asyncio.run(mcp.list_resources())
    return {str(r.uri) for r in resources}


def test_tools_registered() -> None:
    names = _tool_names()
    assert "diagnose_runtime" in names
    assert "list_capabilities" in names


def test_resource_registered() -> None:
    assert "inkscape://runtime/capabilities" in _resource_uris()


def test_list_capabilities_returns_capabilities() -> None:
    caps = list_capabilities()
    assert isinstance(caps, Capabilities)


def test_diagnose_runtime_returns_capabilities() -> None:
    caps = diagnose_runtime()
    assert isinstance(caps, Capabilities)


def test_list_capabilities_and_resource_agree() -> None:
    """Both serve the shared cache, so they must be identical for one probe."""
    # Prime the cache deterministically.
    system_mod.refresh_capabilities()
    tool_caps = list_capabilities()
    resource_caps = Capabilities.model_validate_json(runtime_capabilities())
    assert tool_caps == resource_caps
    # Same probe -> same timestamp (proves a shared cached object, not two probes).
    assert tool_caps.probed_at == resource_caps.probed_at


def test_list_capabilities_reports_authoritative_tool_count() -> None:
    """`tool_count` equals the live registry count, and `tools` is the full surface.

    The single source of truth is `mcp.list_tools()` — the same accessor `gen_llms_txt.py` reads —
    so an agent reads one unambiguous number instead of deriving it (the 98/87/88/91 disagreement).
    """
    registry_names = _tool_names()
    assert registry_names, "expected a non-empty registered tool surface"

    caps = list_capabilities()
    assert caps.tool_count == len(registry_names)
    assert caps.tools, "tools list must be non-empty"
    assert caps.tool_count == len(caps.tools)
    # The reported names are exactly the registered `@mcp.tool`s (not resources/prompts).
    assert {t.name for t in caps.tools} == registry_names
    # Every entry carries a one-line purpose; risk is a canonical token (or 'unknown' if absent).
    assert all(t.purpose for t in caps.tools)
    assert all(t.risk in {"low", "medium", "high", "restricted", "unknown"} for t in caps.tools)


def test_resource_carries_tool_count_field() -> None:
    """the runtime resource mirrors `tool_count` + `tools` and agrees with the tool."""
    system_mod.refresh_capabilities()
    resource_caps = Capabilities.model_validate_json(runtime_capabilities())
    assert resource_caps.tool_count == len(_tool_names())
    assert resource_caps.tool_count == len(resource_caps.tools)
    assert resource_caps.tools
    assert resource_caps == list_capabilities()


def test_diagnose_runtime_refreshes_cache() -> None:
    """`diagnose_runtime` re-probes and updates the cache that the resource serves."""
    first = list_capabilities()
    refreshed = diagnose_runtime()
    # The cache now reflects the fresh probe; the resource agrees with it.
    assert Capabilities.model_validate_json(runtime_capabilities()) == refreshed
    assert refreshed.inkscape_available == first.inkscape_available
    # Capability content should match across probes on a stable host (timestamps differ).
    # Skip the content comparison if either probe hit a transient subprocess timeout, which
    # would legitimately yield different (degraded) results.
    timed_out = any("timed out" in n for n in (*first.notes, *refreshed.notes))
    if not timed_out:
        assert refreshed.actions == first.actions


# ---: stat_artifact / stat_artifacts ---------------------------------


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A configured workspace root; clears the settings cache so the env var takes effect."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    return ws


def test_stat_artifact_matches_hashlib_and_os_stat(workspace: Path) -> None:
    data = b"hello inkscape artifact\n" * 100
    art = workspace / "out.png"
    art.write_bytes(data)

    stat = stat_artifact("out.png")  # workspace-relative path

    assert stat.bytes == art.stat().st_size == len(data)
    assert stat.sha256 == hashlib.sha256(data).hexdigest()
    # The echoed path is workspace-relative (no host path leak).
    assert stat.path == "out.png"


def test_stat_artifact_accepts_absolute_in_sandbox(workspace: Path) -> None:
    art = workspace / "sub" / "icon.svg"
    art.parent.mkdir()
    art.write_bytes(b"<svg/>")
    stat = stat_artifact(str(art))
    assert stat.path == "sub/icon.svg"
    assert stat.sha256 == hashlib.sha256(b"<svg/>").hexdigest()


def test_stat_artifacts_set_returns_per_file_and_total(workspace: Path) -> None:
    files = {"a.png": b"aaaa", "b.png": b"bbbbbb", "c.png": b"c"}
    for name, content in files.items():
        (workspace / name).write_bytes(content)

    result = stat_artifacts(["a.png", "b.png", "c.png"])

    assert result.count == 3
    assert result.total_bytes == sum(len(c) for c in files.values())
    by_path = {s.path: s for s in result.artifacts}
    for name, content in files.items():
        assert by_path[name].bytes == len(content)
        assert by_path[name].sha256 == hashlib.sha256(content).hexdigest()


def test_stat_artifact_out_of_sandbox_rejected(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"secret")
    with pytest.raises(ToolError) as exc:
        stat_artifact(str(outside))
    assert "path rejected: outside workspace" in str(exc.value)


def test_stat_artifact_traversal_escape_rejected(workspace: Path) -> None:
    with pytest.raises(ToolError) as exc:
        stat_artifact("../escape.png")
    assert "path rejected" in str(exc.value)


def test_stat_artifacts_empty_list_rejected(workspace: Path) -> None:
    with pytest.raises(ToolError):
        stat_artifacts([])


def test_stat_tools_registered() -> None:
    names = _tool_names()
    assert "stat_artifact" in names
    assert "stat_artifacts" in names
