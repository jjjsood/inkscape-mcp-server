"""Subprocess execution wrapper (workspace model / sec.12).

HARD RULE: arg lists only, `shell=False`. A shell string is NEVER built. Every argv element
must be a validated/typed value before it reaches this layer; no user string is interpolated
verbatim into argv here.

Timeout policy: on expiry the process is killed and a `ProcessResult` is returned with
`timed_out=True` (the partial stdout/stderr captured up to the kill are preserved). Callers
that prefer a hard failure can inspect `timed_out` and raise. `run_process` does NOT raise on
timeout; it raises `ProcessError` only when the program cannot be launched at all.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from pydantic import BaseModel

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.logging_setup import get_logger

_logger = get_logger("subprocess_exec")


class ProcessResult(BaseModel):
    """Result of a completed (or timed-out) subprocess invocation."""

    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool


class ProcessError(Exception):
    """The subprocess could not be launched (e.g. binary missing)."""


def run_process(
    args: list[str],
    timeout_s: float | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    """Run `args` with `shell=False`, capturing stdout/stderr as text.

    `timeout_s` defaults to `settings.process_timeout_s`. On timeout the process is killed
    and a `ProcessResult` with `timed_out=True` is returned (partial output preserved).
    Raises `ProcessError` if the program cannot be launched.
    """
    if not args:
        raise ProcessError("run_process called with empty argv")

    timeout = timeout_s if timeout_s is not None else get_settings().process_timeout_s
    start = time.monotonic()
    try:
        completed = subprocess.run(
            args,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start
        return ProcessResult(
            args=list(args),
            returncode=-1,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
            duration_s=duration,
            timed_out=True,
        )
    except FileNotFoundError as exc:
        raise ProcessError(f"executable not found: {Path(args[0]).name!r}") from exc
    except OSError as exc:
        raise ProcessError(
            f"failed to launch process {Path(args[0]).name!r}: {exc.strerror or exc}"
        ) from exc

    duration = time.monotonic() - start
    return ProcessResult(
        args=list(args),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_s=duration,
        timed_out=False,
    )


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def inkscape_env() -> dict[str, str]:
    """Environment for a one-shot Inkscape CLI child: no session bus.

    Each CLI start registers a GApplication on the session bus; several starting at once abort
    with ``Gio::DBus::Error`` (SIGABRT). One-shot CLI runs never need the bus (live mode does).
    """
    return {**os.environ, "DBUS_SESSION_BUS_ADDRESS": "disabled:"}


def run_inkscape(args: list[str], settings: Settings | None = None) -> ProcessResult:
    """Run the Inkscape CLI with the per-process timeout enforced.

    Resolves the binary via `shutil.which("inkscape")` and prepends it to `args`. Raises
    `ProcessError("inkscape binary not found")` if absent. A non-timeout failure is logged and
    retried once with the remaining budget (transient crashes under load); timeouts never are.
    """
    s = settings if settings is not None else get_settings()
    binary = shutil.which("inkscape")
    if binary is None:
        raise ProcessError("inkscape binary not found")
    budget = s.process_timeout_s
    result = run_process([binary, *args], timeout_s=budget, env=inkscape_env())
    if result.returncode == 0 and not result.timed_out:
        return result
    left = budget - result.duration_s
    retry = not result.timed_out and left >= 1.0
    _log_failure(result, attempt=1, retry=retry)
    if not retry:
        return result
    # ponytail: one retry for transient crashes (run7 F1); total stays within process_timeout_s
    result = run_process([binary, *args], timeout_s=left, env=inkscape_env())
    if result.returncode != 0 or result.timed_out:
        _log_failure(result, attempt=2, retry=False)
    return result


def _log_failure(r: ProcessResult, *, attempt: int, retry: bool) -> None:
    sig = None
    if r.returncode < 0 and not r.timed_out:
        try:
            sig = signal.Signals(-r.returncode).name
        except ValueError:
            pass
    _logger.warning(
        "inkscape cli failed",
        extra={
            "event": "inkscape_cli",
            "attempt": attempt,
            "retry": retry,
            "timed_out": r.timed_out,
            "returncode": r.returncode,
            "signal": sig,
            "duration_s": round(r.duration_s, 3),
            "stderr_tail": _scrub(r.stderr[-500:]),
        },
    )


def _scrub(text: str) -> str:
    """Absolute host paths -> basename (logs must not leak the workspace layout)."""
    return re.sub(r"(?<![\w.])/[^\s'\":]+", lambda m: Path(m.group()).name, text)
