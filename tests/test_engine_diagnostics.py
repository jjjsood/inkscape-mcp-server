"""shell-mode availability is reported by the probe + the active round-trip checker."""

from __future__ import annotations

import shutil

import pytest

from inkscape_mcp.engine.process import shell_mode_available
from inkscape_mcp.runtime.probe import probe_capabilities

inkscape_available = shutil.which("inkscape") is not None


def test_probe_reports_shell_mode_tracks_inkscape() -> None:
    caps = probe_capabilities()
    # Shell mode ships on every supported Inkscape, so availability tracks the binary's presence.
    assert caps.shell_mode_available == caps.inkscape_available


def test_capabilities_field_round_trips() -> None:
    caps = probe_capabilities()
    from inkscape_mcp.runtime.probe import Capabilities

    again = Capabilities.model_validate_json(caps.model_dump_json())
    assert again.shell_mode_available == caps.shell_mode_available


@pytest.mark.inkscape
def test_active_shell_mode_probe_round_trips() -> None:
    # A real spawn + framed no-op round-trip + clean shutdown.
    assert shell_mode_available() is True
