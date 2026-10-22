"""Action discovery + versioned capability-map tests.

Hermetic: every test feeds a synthetic `Capabilities` so no Inkscape is launched. Covers discovery
returning the probed surface + allowlisted/available subsets and the version-keyed map.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from inkscape_mcp.actions.capability_map import (
    build_action_map,
    discover_actions,
    discover_extensions,
)
from inkscape_mcp.config import Settings
from inkscape_mcp.runtime.probe import Capabilities


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
    # S4: the empty default is explained in notes, not left as a bare `[]`.
    assert any("opt-in" in n and "default" in n for n in d.notes)


# --- versioned capability map ----------------------------------------------


def test_build_action_map_keys_by_version() -> None:
    m = build_action_map(_caps(["select-by-id", "path-union"]))
    assert m.inkscape_version == "Inkscape 1.4.3 (abc, 2025)"
    assert m.has("path-union") is True
    assert m.has("path-intersection") is False
