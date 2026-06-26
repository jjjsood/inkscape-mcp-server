"""Shell-command composition for the warm engine (E12-03 / E12-04 / ADR-007).

The single place where engine ops are expressed as `inkscape --shell` command lines. Each function
runs through the :class:`EngineManager` (warm, serialized, freshness-checked worker) and raises an
:class:`EngineError` on any fault so the caller can fall back to the per-call CLI — correctness can
never regress.

Sticky-state discipline (the E12-02 spike finding): the shell keeps export options across commands
(``file-open`` does NOT reset them), so every export here sets its FULL option set explicitly,
including ``export-width:0`` to neutralize a sticky width — making each export deterministic and
byte-equivalent to the per-call CLI regardless of what ran before (verified on 1.4.3). Only
WHOLE-DOCUMENT PNG/SVG go through the warm worker; object/PDF exports keep the per-call path (sticky
``export-area-page`` / ``export-id-only`` cannot be reset cleanly — out of scope, falls back).

SECURITY (sec.12): paths sent to the worker are server-controlled (registry working copy / a
server-minted temp file). A path carrying a shell-grammar metacharacter (``;`` separates actions,
newline desyncs framing) is REFUSED here and the caller falls back to the per-call CLI (whose argv
handles any path). Action tokens are validated by the caller (E6 allowlist + map + charset)
before they reach this layer; this transport adds no new authority.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.engine.manager import get_engine_manager
from inkscape_mcp.engine.process import EngineCrash, EngineProcess, EngineUnavailable
from inkscape_mcp.logging_setup import get_logger
from inkscape_mcp.workspace.limits import LimitExceeded, check_output_size
from inkscape_mcp.workspace.xml_safety import UnsafeXMLError, parse_svg_bytes

_logger = get_logger("engine.ops")

#: Whole-document export formats the warm worker handles (object/PDF fall back to per-call).
ENGINE_EXPORT_FORMATS = frozenset({"png", "svg"})


def _assert_shell_safe_path(path: Path) -> None:
    """Refuse a path carrying a shell-grammar metacharacter (caller then uses the per-call CLI)."""
    text = str(path)
    if ";" in text or "\n" in text or "\r" in text:
        raise EngineUnavailable("path not representable as a shell command (uses per-call)")


def _export_line(out_path: Path, *, fmt: str, width_px: int | None) -> str:
    """Build a deterministic, self-neutralizing whole-doc export command line."""
    parts = [f"export-type:{fmt}"]
    if fmt == "svg":
        parts.append("export-plain-svg")
    # Always set width explicitly (0 = intrinsic) to neutralize a sticky prior export-width.
    parts.append(f"export-width:{int(width_px) if width_px else 0}")
    parts.append("export-area-page")
    parts.append(f"export-filename:{out_path}")
    parts.append("export-do")
    return "; ".join(parts)


def engine_export_document(
    working_path: Path,
    out_path: Path,
    *,
    fmt: str,
    width_px: int | None,
    settings: Settings | None = None,
) -> None:
    """Export the whole document to `out_path` (PNG/SVG) via the warm worker.

    Writes the artifact in place (the caller owns naming + post-checks). Raises
    :class:`EngineError` on any fault — missing binary, crash, timeout, unknown action, or a missing
    output file — so the caller falls back to the per-call CLI.
    """
    s = settings if settings is not None else get_settings()
    if fmt not in ENGINE_EXPORT_FORMATS:  # pragma: no cover - guarded by the caller
        raise EngineUnavailable(f"format {fmt!r} not handled by the warm engine")
    _assert_shell_safe_path(working_path)
    _assert_shell_safe_path(out_path)
    line = _export_line(out_path, fmt=fmt, width_px=width_px)

    def fn(proc: EngineProcess) -> None:
        proc.execute(line, timeout_s=s.process_timeout_s)

    get_engine_manager().run(working_path, fn)
    if not out_path.exists():
        raise EngineCrash("warm engine produced no output")


def engine_run_actions(
    working_path: Path,
    actions_argument: str,
    *,
    settings: Settings | None = None,
) -> bytes:
    """Run a validated action line on the warm, stateful worker and return the result SVG bytes.

    `actions_argument` is the SAME string the per-call path passes to ``--actions`` (e.g.
    ``select-by-id:p_a,p_b;path-union``); it is already E6-validated (allowlist + capability map +
    charset) by the caller. The worker reloads the on-disk working copy first (``force_reopen``) so
    its in-memory document never diverges from disk, runs the action line, then exports the mutated
    document to a server-minted temp file as plain SVG. The bytes are size-capped and re-parsed
    through the SAFE parser before return; the temp file is always removed. Raises
    :class:`EngineError` on any engine fault (caller falls back to the per-call CLI) and re-raises a
    size/unsafe-output failure so a bad result never reaches the working copy.
    """
    s = settings if settings is not None else get_settings()
    _assert_shell_safe_path(working_path)
    fd, tmp_name = tempfile.mkstemp(prefix="inkscape-mcp-engine-", suffix=".svg")
    out_path = Path(tmp_name)
    try:
        os.close(fd)
        _assert_shell_safe_path(out_path)
        export_line = _export_line(out_path, fmt="svg", width_px=None)

        def fn(proc: EngineProcess) -> None:
            # Run the action line FIRST so an unknown action raises BEFORE we export (otherwise the
            # export would persist the UNMUTATED document — a silent wrong result).
            proc.execute(actions_argument, timeout_s=s.process_timeout_s)
            proc.execute(export_line, timeout_s=s.process_timeout_s)

        get_engine_manager().run(working_path, fn, force_reopen=True)
        if not out_path.exists():
            raise EngineCrash("warm engine produced no output")

        try:
            check_output_size(out_path, s)
        except LimitExceeded as exc:
            raise EngineCrash("warm engine output exceeds size limit") from exc

        data = out_path.read_bytes()
        if not data.strip():
            raise EngineCrash("warm engine produced empty output")
        try:
            parse_svg_bytes(data)
        except UnsafeXMLError as exc:
            raise EngineCrash("warm engine produced unsafe output") from exc
        return data
    finally:
        try:
            out_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best-effort cleanup
            pass


def engine_mode_is_shell(settings: Settings | None = None) -> bool:
    """Whether the operator selected the warm shell engine transport (`engine_mode == shell`)."""
    from inkscape_mcp.config import ENGINE_MODE_SHELL

    s = settings if settings is not None else get_settings()
    return s.engine_mode == ENGINE_MODE_SHELL


__all__ = [
    "ENGINE_EXPORT_FORMATS",
    "engine_export_document",
    "engine_mode_is_shell",
    "engine_run_actions",
]
