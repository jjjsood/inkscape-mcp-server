"""Subprocess-wrapper tests (workspace-model.md §4 / sec.12)."""

from __future__ import annotations

import sys

import pytest

from inkscape_mcp.config import ENV_PROCESS_TIMEOUT_S, get_settings
from inkscape_mcp.workspace import subprocess_exec
from inkscape_mcp.workspace.subprocess_exec import (
    ProcessError,
    ProcessResult,
    run_inkscape,
    run_process,
)


def test_arg_list_echo_works() -> None:
    result = run_process([sys.executable, "-c", "print('hello')"], timeout_s=10)
    assert isinstance(result, ProcessResult)
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"
    assert result.timed_out is False
    assert result.args[0] == sys.executable


def test_shell_metacharacters_passed_literally() -> None:
    """argv is never shell-interpreted: `$(id);rm x` reaches the child verbatim."""
    payload = "$(id);rm x"
    result = run_process([sys.executable, "-c", "import sys;print(sys.argv[1])", payload], 10)
    assert result.stdout.rstrip("\n") == payload


def test_nonzero_returncode_captured() -> None:
    result = run_process(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
        timeout_s=10,
    )
    assert result.returncode == 3
    assert "boom" in result.stderr
    assert result.timed_out is False


def test_timeout_sets_flag() -> None:
    # Sleep longer than the timeout: process is killed and timed_out flagged.
    result = run_process(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        timeout_s=0.2,
    )
    assert result.timed_out is True
    assert result.returncode == -1


def test_empty_argv_raises() -> None:
    with pytest.raises(ProcessError):
        run_process([])


def test_missing_binary_raises_process_error() -> None:
    with pytest.raises(ProcessError):
        run_process(["this-binary-does-not-exist-xyz", "--version"], timeout_s=5)


def test_run_inkscape_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("inkscape_mcp.workspace.subprocess_exec.shutil.which", lambda _name: None)
    with pytest.raises(ProcessError, match="inkscape binary not found"):
        run_inkscape(["--version"])


def test_run_inkscape_uses_the_configured_process_timeout(monkeypatch) -> None:
    monkeypatch.setenv(ENV_PROCESS_TIMEOUT_S, "7.5")
    get_settings.cache_clear()
    seen: dict[str, object] = {}

    def run_process(argv: list[str], timeout_s: float, env: object = None) -> ProcessResult:
        seen.update(argv=argv, timeout_s=timeout_s)
        return ProcessResult(
            args=argv, returncode=0, stdout="", stderr="", duration_s=0.0, timed_out=False
        )

    monkeypatch.setattr(subprocess_exec.shutil, "which", lambda _name: "/opt/inkscape")
    monkeypatch.setattr(subprocess_exec, "run_process", run_process)
    assert subprocess_exec.run_inkscape(["--version"]).returncode == 0
    assert seen == {"argv": ["/opt/inkscape", "--version"], "timeout_s": 7.5}


@pytest.mark.inkscape
def test_run_inkscape_version() -> None:
    result = run_inkscape(["--version"])
    assert result.returncode == 0
    assert "Inkscape" in result.stdout
