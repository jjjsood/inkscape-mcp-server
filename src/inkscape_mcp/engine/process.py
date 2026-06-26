"""One long-lived `inkscape --shell` worker (ADR-007).

`EngineProcess` wraps a single persistent `inkscape --shell` subprocess so the Inkscape-engine ops
(render / export / path / boolean / action-chain) run against a WARM, stateful process instead of
paying the ~0.16 s cold process start of a fresh `inkscape …` per call. It is a PRIVATE headless
worker the server spawns and supervises — Inkscape is built ``Gio::APPLICATION_NON_UNIQUE`` so this
process is its own instance on its own document (arch §4.4); it never attaches to or drives the
user's running GUI.

Framing (the spike finding, verified on Inkscape 1.4.3). The shell has no machine-readable
per-command delimiter; instead it ECHOES each command on its own line, prints any output lines, then
emits a bare ``"> "`` prompt with NO trailing newline when it is ready for the next command. Real
output lines always end in ``\\n``; the prompt is the only token that does not — so the response for
one command is "everything up to and including the next ``\\n> ``". This module reads stdout in a
background thread and frames each command on that ``"\\n> "`` sentinel (plus the banner prompt at
startup). Action errors do NOT appear on stdout: an unknown action prints
``InkscapeApplication::parse_actions: could not find action for: <X>`` to STDERR, so a second reader
thread captures stderr and :func:`EngineProcess.execute` maps that line to a clean, host-path-free
error.

SECURITY (sec.12 / X1): the worker is spawned as an ARG LIST with ``shell=False`` (no shell string
is ever built); the spawn argv is fixed (``[inkscape, "--shell"]``) — no client value in it. Every
command written to the worker's stdin is built by the caller from validated/typed tokens (the same
allowlist + map + charset gates as the per-call path; this layer adds no new authority).
Each command has a per-command timeout; on expiry (or any IO fault) the worker is killed so a
hung engine can never block the server. The worker is reaped on idle-timeout and on shutdown.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time

from pydantic import BaseModel

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.logging_setup import get_logger

_logger = get_logger("engine.process")

#: The bare shell prompt emitted when the worker is ready for the next command. It has NO trailing
#: newline, which is what makes it a reliable response delimiter (every real output line ends in a
#: newline; only the prompt does not). Framing reads stdout until it ends with ``"\n" + PROMPT`` (a
#: command's echo line always precedes it) or equals the lone banner prompt.
PROMPT = "> "

#: The plaintext stderr line Inkscape prints for an unknown/misspelled action. Detected and
#: surfaced as a clean :class:`EngineActionError` (mirrors the "action_absent"
#: class) so a bad action never silently no-ops.
_PARSE_ERROR_RE = re.compile(
    r"InkscapeApplication::parse_actions: could not find action for:\s*(?P<action>.+)"
)


class EngineError(Exception):
    """Base class for headless-shell engine faults (caller falls back to the per-call CLI)."""


class EngineUnavailable(EngineError):
    """The Inkscape binary is missing or the worker could not be spawned/started."""


class EngineTimeout(EngineError):
    """A command exceeded its per-command timeout; the worker has been killed."""


class EngineCrash(EngineError):
    """The worker exited/closed its pipe unexpectedly (mid-command or between commands)."""


class EngineActionError(EngineError):
    """The worker reported a plaintext action error (e.g. an unknown action id)."""


class EngineResponse(BaseModel):
    """The framed result of one shell command.

    `output_lines` is the worker's stdout for the command with the echoed command line and the
    trailing prompt stripped (empty for a command that prints nothing, e.g. ``select-by-id``).
    `stderr` is whatever the worker wrote to stderr during the command window (diagnostic only).
    """

    command: str
    output_lines: list[str]
    stderr: str


class EngineProcess:
    """A supervised, single-threaded `inkscape --shell` worker.

    One command runs at a time (the shell is single-threaded); the owning :class:`EngineManager`
    serializes access. Construct with the spawn argv (defaulting to the resolved Inkscape binary +
    ``--shell``); tests inject a fake-shell argv to exercise the real framing/threading/lifecycle
    code with no Inkscape present.
    """

    def __init__(
        self,
        argv: list[str] | None = None,
        *,
        settings: Settings | None = None,
        label: str = "",
    ) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._argv = list(argv) if argv is not None else self._default_argv()
        self._label = label
        self._proc: subprocess.Popen[bytes] | None = None
        #: Shared stdout buffer (decoded), grown by the reader thread, framed by ``execute``.
        self._out = ""
        self._consumed = 0
        self._err = ""
        self._eof = False
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._readers: list[threading.Thread] = []
        self._last_used = time.monotonic()
        #: The working-copy path currently `file-open`-ed in this worker (tracked for freshness).
        self.opened_path: str | None = None
        self.opened_mtime_ns: int = 0
        self.opened_size: int = 0

    @staticmethod
    def _default_argv() -> list[str]:
        binary = shutil.which("inkscape")
        if binary is None:
            raise EngineUnavailable("inkscape binary not found")
        return [binary, "--shell"]

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Spawn the worker (arg list, ``shell=False``) and drain its banner to the prompt."""
        if self._proc is not None:
            return
        try:
            self._proc = subprocess.Popen(
                self._argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                bufsize=0,
            )
        except (OSError, ValueError) as exc:
            raise EngineUnavailable(f"could not spawn shell worker: {exc}") from exc

        self._readers = [
            threading.Thread(target=self._read_stdout, daemon=True),
            threading.Thread(target=self._read_stderr, daemon=True),
        ]
        for t in self._readers:
            t.start()

        # Drain the startup banner up to the first prompt so the first command frames cleanly.
        self._wait_for_prompt(self._settings.process_timeout_s, draining_banner=True)
        self._last_used = time.monotonic()

    def _read_stdout(self) -> None:
        if self._proc is None or self._proc.stdout is None:  # pragma: no cover - set before start
            return
        fd = self._proc.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                with self._cond:
                    self._eof = True
                    self._cond.notify_all()
                return
            with self._cond:
                self._out += chunk.decode("utf-8", errors="replace")
                self._cond.notify_all()

    def _read_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:  # pragma: no cover - set before start
            return
        fd = self._proc.stderr.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                return
            with self._cond:
                self._err += chunk.decode("utf-8", errors="replace")
                self._cond.notify_all()

    def is_alive(self) -> bool:
        """True iff the worker process is spawned and has not exited."""
        return self._proc is not None and self._proc.poll() is None and not self._eof

    def idle_seconds(self) -> float:
        """Seconds since the last command completed (for idle reaping)."""
        return time.monotonic() - self._last_used

    def shutdown(self, timeout_s: float = 5.0) -> None:
        """Send ``quit`` for a clean exit; kill the worker if it does not exit in time."""
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin is not None:
                try:
                    proc.stdin.write(b"quit\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    pass
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                self._kill()
        finally:
            self._close_streams()
            self._proc = None

    def _kill(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - best-effort
            pass

    def _close_streams(self) -> None:
        proc = self._proc
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:  # pragma: no cover - best-effort
                pass

    # --- command execution --------------------------------------------------

    def execute(self, command: str, timeout_s: float | None = None) -> EngineResponse:
        """Run one shell `command`, frame its response, and return it.

        Writes ``command + "\\n"`` to the worker's stdin and reads stdout until the next prompt
        sentinel. Raises :class:`EngineCrash` if the worker died, :class:`EngineTimeout` (after
        killing the worker) if the prompt does not arrive within the per-command timeout, and
        :class:`EngineActionError` if the worker reported an unknown-action error on stderr. The
        command must already be composed from validated/typed tokens by the caller (sec.12).
        """
        if "\n" in command or "\r" in command:
            # A newline would be read as TWO commands and desync framing — refuse defensively.
            raise EngineActionError("shell command must not contain a newline")
        proc = self._proc
        if proc is None or proc.stdin is None or not self.is_alive():
            raise EngineCrash("shell worker is not running")

        timeout = timeout_s if timeout_s is not None else self._settings.process_timeout_s
        with self._cond:
            err_mark = len(self._err)
        try:
            proc.stdin.write((command + "\n").encode("utf-8"))
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise EngineCrash(f"shell worker pipe closed: {exc}") from exc

        frame = self._wait_for_prompt(timeout)
        with self._cond:
            stderr_window = self._err[err_mark:]

        output_lines = self._strip_frame(frame, command)
        self._check_action_error(stderr_window)
        self._last_used = time.monotonic()
        return EngineResponse(command=command, output_lines=output_lines, stderr=stderr_window)

    def _wait_for_prompt(self, timeout_s: float, *, draining_banner: bool = False) -> str:
        """Block until the framed region (stdout since `_consumed`) ends with the prompt sentinel.

        Returns the framed region (echo + output + prompt) and advances `_consumed` past it. On
        timeout the worker is killed and :class:`EngineTimeout` is raised; on EOF (worker exit),
        :class:`EngineCrash`.
        """
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while True:
                region = self._out[self._consumed :]
                if _ends_with_prompt(region):
                    self._consumed = len(self._out)
                    return region
                if self._eof:
                    raise EngineCrash("shell worker exited before responding")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
        # Timed out: kill the worker so a hung engine never blocks the server.
        self._kill()
        with self._cond:
            self._eof = True
        phase = "banner" if draining_banner else "command"
        _logger.error("shell worker timed out", extra={"phase": phase, "label": self._label})
        raise EngineTimeout(f"shell worker timed out waiting for {phase} response")

    @staticmethod
    def _strip_frame(frame: str, command: str) -> list[str]:
        """Strip the echoed command line and the trailing prompt from a framed response region."""
        body = frame
        # Drop the trailing prompt (``...\n> `` or a bare ``> ``).
        if body.endswith(PROMPT):
            body = body[: -len(PROMPT)]
        body = body.rstrip("\n")
        if not body:
            return []
        lines = body.split("\n")
        # The worker echoes the command back as the first line; drop it when it matches.
        if lines and lines[0] == command:
            lines = lines[1:]
        return lines

    def _check_action_error(self, stderr_window: str) -> None:
        for line in stderr_window.splitlines():
            match = _PARSE_ERROR_RE.search(line)
            if match is not None:
                action = match.group("action").strip()
                raise EngineActionError(f"unknown action: {action!r}")


def _ends_with_prompt(region: str) -> bool:
    """Whether `region` ends with the response prompt (the command-response delimiter).

    True when the region ends with ``"\\n> "`` (a command's echo/output precedes the prompt) or is
    exactly the bare prompt ``"> "`` (the startup banner ends ``"...\\n> "`` and is handled by the
    first branch; the bare form is a defensive fallback).
    """
    return region.endswith("\n" + PROMPT) or region == PROMPT


__all__ = [
    "PROMPT",
    "EngineActionError",
    "EngineCrash",
    "EngineError",
    "EngineProcess",
    "EngineResponse",
    "EngineTimeout",
    "EngineUnavailable",
]


def _which_inkscape() -> str | None:  # pragma: no cover - thin shim, used by manager/diagnostics
    return shutil.which("inkscape")


def shell_mode_available(settings: Settings | None = None) -> bool:
    """Best-effort: whether a `inkscape --shell` worker can be spawned + framed on this host.

    Spawns a throwaway worker, drains the banner, runs a trivial round-trip, and shuts it down.
    Returns False (never raises) when Inkscape is absent or the shell does not frame cleanly — used
    by the diagnostics layer (arch §4.7 "Is Shell Mode available?") and as a pre-flight before the
    manager commits to shell mode. Bounded by the per-process timeout.
    """
    s = settings if settings is not None else get_settings()
    if _which_inkscape() is None:
        return False
    proc = EngineProcess(settings=s, label="probe")
    try:
        proc.start()
        # A no-op round-trip: an empty line just re-emits the prompt and proves framing works.
        proc.execute("", timeout_s=s.process_timeout_s)
        return True
    except EngineError:
        return False
    finally:
        proc.shutdown()
