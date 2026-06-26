"""Action discovery + versioned capability-map tests (E6-02).

Hermetic: every test feeds a synthetic `Capabilities` so no Inkscape is launched. Covers discovery
returning the probed surface + allowlisted/available subsets, the version-keyed map persisting under
the sandbox `.inkscape-mcp/action-maps/` area + being consulted (graceful when an Action is absent),
the unsafe-version-key guard, and the low-risk discovery tool registration.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from inkscape_mcp.actions import capability_map as cmap
from inkscape_mcp.actions.capability_map import (
    UNKNOWN_VERSION,
    ActionCapabilityMap,
    build_action_map,
    discover_actions,
    discover_extensions,
    get_or_build_action_map,
    load_action_map,
    persist_action_map,
)
from inkscape_mcp.config import Settings
from inkscape_mcp.runtime.probe import Capabilities
from inkscape_mcp.server import mcp
from inkscape_mcp.workspace import sandbox


def _caps(actions: list[str], version: str | None = "Inkscape 1.4.3 (abc, 2025)") -> Capabilities:
    return Capabilities(
        inkscape_available=version is not None,
        inkscape_binary="/usr/bin/inkscape" if version else None,
        inkscape_version=version,
        inkscape_version_tuple=(1, 4, 3) if version else None,
        meets_minimum=bool(version),
        actions=actions,
        python_version="3.12.0",
        probed_at=datetime.now(UTC).isoformat(),
        notes=[] if version else ["inkscape not found on PATH"],
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(workspace_roots=[tmp_path])


# --- discovery (read-only) --------------------------------------------------


def test_discover_actions_returns_probed_surface(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    caps = _caps(["select-by-id", "path-union", "effect.voronoi"])
    d = discover_actions(capabilities=caps, settings=s)
    assert d.inkscape_available is True
    assert d.action_count == 3
    assert "effect.voronoi" in d.actions
    # `available` = allowlisted AND present on host; the non-allowlisted effect.* is excluded.
    assert "select-by-id" in d.available
    assert "path-union" in d.available
    assert "effect.voronoi" not in d.available
    # `allowlisted` reflects the server-side set, independent of the host surface.
    assert "object-to-path" in d.allowlisted


def test_discover_actions_degrades_without_inkscape(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    d = discover_actions(capabilities=_caps([], version=None), settings=s)
    assert d.inkscape_available is False
    assert d.actions == []
    assert d.available == []  # nothing present on host ⇒ nothing available
    assert any("inkscape not found" in n for n in d.notes)


def test_discover_extensions_reports_empty_default_allowlist(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    d = discover_extensions(capabilities=_caps(["select-by-id"]), settings=s)
    # No extension is enabled for execution by default (MVP).
    assert d.allowlisted == []
    # E10-10 S4: the empty default is explained in notes, not left as a bare `[]`.
    assert any("opt-in" in n and "default" in n for n in d.notes)


# --- versioned capability map ----------------------------------------------


def test_build_action_map_keys_by_version() -> None:
    m = build_action_map(_caps(["select-by-id", "path-union"]))
    assert m.inkscape_version == "Inkscape 1.4.3 (abc, 2025)"
    assert m.action_count == 2
    assert m.has("path-union") is True
    assert m.has("path-intersection") is False


def test_persist_and_load_round_trip(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    m = build_action_map(_caps(["select-by-id", "object-to-path"]))
    path = persist_action_map(m, settings=s)
    assert path is not None
    # Filename keyed by the compact version (1.4.3), under the sandbox action-maps dir.
    assert path.parent == sandbox.action_maps_dir(tmp_path)
    assert path.name == "1.4.3.json"
    loaded = load_action_map("Inkscape 1.4.3 (abc, 2025)", settings=s)
    assert loaded is not None
    assert loaded.has("object-to-path") is True
    # On-disk JSON is the model dump.
    raw = json.loads(path.read_text())
    assert raw["action_count"] == 2


def test_persist_no_root_returns_none() -> None:
    s = Settings(workspace_roots=[])
    assert persist_action_map(build_action_map(_caps(["select-by-id"])), settings=s) is None


def test_load_unknown_version_returns_none(tmp_path: Path) -> None:
    assert load_action_map("9.9.9", settings=_settings(tmp_path)) is None


def test_get_or_build_persists_on_miss_then_reads_back(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    caps = _caps(["select-by-id", "path-difference"])
    # First call: cache miss ⇒ build + persist.
    first = get_or_build_action_map(capabilities=caps, settings=s)
    assert first.has("path-difference") is True
    assert (sandbox.action_maps_dir(tmp_path) / "1.4.3.json").is_file()
    # Second call reads the persisted map back (same content).
    second = get_or_build_action_map(capabilities=caps, settings=s)
    assert second.actions == first.actions


def test_unsafe_version_key_degrades_to_unknown(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    # A crafted version with no parseable number and path-hostile chars must not traverse.
    m = ActionCapabilityMap(
        inkscape_version="../../etc/passwd",
        actions=["select-by-id"],
        action_count=1,
        probed_at=datetime.now(UTC).isoformat(),
    )
    path = persist_action_map(m, settings=s)
    assert path is not None
    # Keyed by the unknown sentinel, not the crafted string.
    assert path.name == f"{UNKNOWN_VERSION}.json"
    assert path.parent == sandbox.action_maps_dir(tmp_path)


def test_corrupt_map_file_degrades_to_none(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    sandbox.ensure_action_maps_dir(tmp_path)
    (sandbox.action_maps_dir(tmp_path) / "1.4.3.json").write_text("{not json")
    assert load_action_map("1.4.3", settings=s) is None


# --- tool registration ------------------------------------------------------


def test_discovery_tools_registered() -> None:
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "list_actions" in names
    assert "discover_extensions" in names


def test_version_key_helper() -> None:
    assert cmap._version_key("Inkscape 1.4.3 (abc)") == "1.4.3"
    assert cmap._version_key("Inkscape 1.2") == "1.2"
    assert cmap._version_key(None) == UNKNOWN_VERSION
    assert cmap._version_key("../evil") == UNKNOWN_VERSION


# --- E13-05: list_actions can omit the bulky full action array -------------------


def test_list_actions_include_all_actions_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    """include_all_actions=False drops the big `actions` array but keeps the count + subsets."""
    from inkscape_mcp.actions.capability_map import ActionDiscovery
    from inkscape_mcp.tools import actions as actions_tool

    sample = ActionDiscovery(
        inkscape_available=True,
        inkscape_version="Inkscape 1.4.3",
        actions=[f"act-{i}" for i in range(1006)],
        action_count=1006,
        allowlisted=["path-union", "select-by-id"],
        available=["path-union", "select-by-id"],
    )
    monkeypatch.setattr(actions_tool, "_discover_actions", lambda: sample)
    monkeypatch.setattr(actions_tool, "persist_action_map", lambda *a, **k: None)
    monkeypatch.setattr(actions_tool, "get_or_build_action_map", lambda *a, **k: None)

    full = actions_tool.list_actions()
    assert len(full.actions) == 1006  # default keeps back-compat (full list)

    slim = actions_tool.list_actions(include_all_actions=False)
    assert slim.actions == []
    assert slim.action_count == 1006  # the host total stays truthful
    assert slim.allowlisted == ["path-union", "select-by-id"]
    assert slim.available == ["path-union", "select-by-id"]
