"""shell-mode availability: the active round-trip checker against a real binary."""

from __future__ import annotations

import pytest

from inkscape_mcp.engine.process import shell_mode_available


@pytest.mark.inkscape
def test_active_shell_mode_probe_round_trips() -> None:
    # A real spawn + framed no-op round-trip + clean shutdown.
    assert shell_mode_available() is True
