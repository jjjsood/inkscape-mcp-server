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

import shutil
import subprocess
import time
from pathlib import Path

from pydantic import BaseModel

from inkscape_mcp.config import Settings, get_settings


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


def run_inkscape(args: list[str], settings: Settings | None = None) -> ProcessResult:
    """Run the Inkscape CLI with the per-process timeout enforced.

    Resolves the binary via `shutil.which("inkscape")` and prepends it to `args`. Raises
    `ProcessError("inkscape binary not found")` if absent.
    """
    s = settings if settings is not None else get_settings()
    binary = shutil.which("inkscape")
    if binary is None:
        raise ProcessError("inkscape binary not found")
    return run_process([binary, *args], timeout_s=s.process_timeout_s)
