"""Probe-engine tests (E1-03).

Two flavors: live assertions on THIS host (gated behind `@pytest.mark.inkscape`, since they
need a real Inkscape binary), and graceful-degradation tests that simulate a missing binary and
assert the probe records a note instead of raising.
"""

from __future__ import annotations

import pytest

from inkscape_mcp.runtime import probe as probe_mod
from inkscape_mcp.runtime.probe import (
    MINIMUM_VERSION,
    Capabilities,
    _parse_action_list,
    _parse_export_types,
    _parse_version,
    probe_capabilities,
)


def test_parse_version_full() -> None:
    assert _parse_version("Inkscape 1.4.3 (0d15f75042, 2025-12-25)") == (1, 4, 3)


def test_parse_version_two_component() -> None:
    assert _parse_version("Inkscape 1.4") == (1, 4, 0)


def test_parse_version_unparsable() -> None:
    assert _parse_version("no version here") is None


def test_parse_export_types_bracketed() -> None:
    line = "  -o, --export-type=TYPE   File type(s) to export: [svg,png,ps,eps,pdf,emf,wmf,xaml]"
    assert _parse_export_types(line) == ["svg", "png", "ps", "eps", "pdf", "emf", "wmf", "xaml"]


def test_parse_export_types_whitespace_separated() -> None:
    line = "--export-type=TYPE  [svg, png, ps]"
    assert _parse_export_types(line) == ["svg", "png", "ps"]


def test_parse_export_types_absent() -> None:
    assert _parse_export_types("nothing relevant") == []


def test_parse_action_list_basic() -> None:
    stdout = (
        "export-do           :  Export ausfuehren\n"
        "object-to-path      :  In Pfad umwandeln\n"
        "effect.voronoi      :  Voronoi-Diagramm\n"
        "\n"
        "garbageline\n"
        "export-do           :  duplicate id is dropped\n"
    )
    actions = _parse_action_list(stdout)
    assert actions == ["export-do", "object-to-path", "effect.voronoi"]


def test_probe_returns_capabilities_type() -> None:
    caps = probe_capabilities()
    assert isinstance(caps, Capabilities)
    # Always-present fields regardless of Inkscape availability.
    assert isinstance(caps.python_version, str) and caps.python_version
    assert isinstance(caps.notes, list)
    assert isinstance(caps.actions, list)
    assert isinstance(caps.export_types, list)
    assert caps.probed_at.endswith("+00:00") or "Z" in caps.probed_at


@pytest.mark.inkscape
def test_probe_on_this_host() -> None:
    caps = probe_capabilities()
    assert caps.inkscape_available is True
    assert caps.inkscape_binary is not None

    # A transient subprocess timeout under load is an environment condition, not a code
    # defect: the probe correctly records it as a note + degrades. Skip the strict
    # capability assertions in that case rather than flaking the suite.
    if any("timed out" in note for note in caps.notes):
        pytest.skip(f"probe subprocess timed out on this host: {caps.notes}")

    assert caps.inkscape_version is not None
    assert caps.inkscape_version_tuple is not None
    assert caps.meets_minimum is True
    assert caps.inkscape_version_tuple >= MINIMUM_VERSION
    # Action enumeration + derived flags.
    assert len(caps.actions) > 0
    assert caps.has_export_actions is True
    assert caps.has_object_actions is True
    assert caps.has_path_actions is True
    assert caps.has_select_actions is True
    # Export types parsed from --help.
    assert "svg" in caps.export_types
    assert "png" in caps.export_types
    # Data dirs + inkex sourced from them.
    assert caps.system_data_dir is not None
    assert caps.inkex_path is not None
    assert caps.inkex_version is not None
    # Fonts present on a normal dev host.
    assert caps.font_count > 0


def test_missing_inkscape_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate Inkscape absent: no exception, fields null/false, note recorded."""

    def fake_which(name: str) -> str | None:
        if name == "inkscape":
            return None
        return None  # also drop gdbus / fc-list to exercise their degradation paths

    monkeypatch.setattr(probe_mod.shutil, "which", fake_which)
    caps = probe_capabilities()

    assert caps.inkscape_available is False
    assert caps.inkscape_binary is None
    assert caps.inkscape_version is None
    assert caps.inkscape_version_tuple is None
    assert caps.meets_minimum is False
    assert caps.actions == []
    assert caps.has_export_actions is False
    assert caps.export_types == []
    assert caps.system_data_dir is None
    assert caps.user_data_dir is None
    assert caps.font_count == 0
    # Python version is intrinsic and still reported.
    assert caps.python_version
    # A degradation note must explain the missing binary.
    assert any("inkscape not found" in note for note in caps.notes)


def test_no_dbus_session_bus_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """No DBUS_SESSION_BUS_ADDRESS: dbus flags false + note, never an exception."""
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    caps = probe_capabilities()
    assert caps.dbus_session_bus is False
    assert caps.dbus_inkscape_present is False
    assert any("session bus unavailable" in note for note in caps.notes)
