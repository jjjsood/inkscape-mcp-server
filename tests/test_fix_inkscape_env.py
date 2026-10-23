"""One-shot Inkscape CLI children run without the session bus (concurrent starts abort on it)."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from inkscape_mcp.workspace import subprocess_exec


def test_run_inkscape_disables_session_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kw)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess_exec.shutil, "which", lambda _n: "/usr/bin/inkscape")
    monkeypatch.setattr(subprocess_exec.subprocess, "run", fake_run)
    subprocess_exec.run_inkscape(["--version"])
    subprocess_exec.run_process(["/bin/true"])
    assert seen[0]["env"]["DBUS_SESSION_BUS_ADDRESS"] == "disabled:"
    assert seen[1]["env"] is None  # other children (gdbus for live mode) keep the bus
