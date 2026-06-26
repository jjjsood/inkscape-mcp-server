"""`EngineProcess` framing + lifecycle, exercised against a fake shell (no Inkscape).

The fake (``fake_inkscape_shell.py``) reproduces the real shell's framing, so these tests drive the
PRODUCTION framing / threading / timeout / crash / shutdown code without a real Inkscape binary.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from inkscape_mcp.config import Settings
from inkscape_mcp.engine.process import (
    EngineActionError,
    EngineCrash,
    EngineProcess,
    EngineTimeout,
)

FAKE = [sys.executable, str(Path(__file__).parent / "fake_inkscape_shell.py")]


def _proc(timeout_s: float = 10.0) -> EngineProcess:
    return EngineProcess(argv=FAKE, settings=Settings(process_timeout_s=timeout_s))


def test_banner_drained_and_first_command_frames_cleanly() -> None:
    p = _proc()
    p.start()
    try:
        resp = p.execute("select-by-id:r1")
        # No-output command: echo + prompt stripped leaves nothing.
        assert resp.output_lines == []
        assert resp.command == "select-by-id:r1"
    finally:
        p.shutdown()


def test_single_line_output_stripped_of_echo_and_prompt() -> None:
    p = _proc()
    p.start()
    try:
        assert p.execute("query-x").output_lines == ["10"]
        assert p.execute("query-width").output_lines == ["30"]
    finally:
        p.shutdown()


def test_multiline_output_framed_without_bleeding_into_next_command() -> None:
    p = _proc()
    p.start()
    try:
        resp = p.execute("query-all")
        assert resp.output_lines == ["svg1,10,10,70,20", "r1,10,10,30,20"]
        # The next command must frame cleanly (no leftover from the multi-line response).
        assert p.execute("query-x").output_lines == ["10"]
    finally:
        p.shutdown()


def test_unknown_action_surfaces_engine_action_error() -> None:
    p = _proc()
    p.start()
    try:
        with pytest.raises(EngineActionError) as ei:
            p.execute("definitely-not-an-action")
        assert "definitely-not-an-action" in str(ei.value)
    finally:
        p.shutdown()


def test_newline_in_command_is_refused() -> None:
    p = _proc()
    p.start()
    try:
        with pytest.raises(EngineActionError):
            p.execute("query-x\nquery-width")
    finally:
        p.shutdown()


def test_per_command_timeout_kills_worker() -> None:
    p = _proc(timeout_s=0.5)
    p.start()
    try:
        with pytest.raises(EngineTimeout):
            p.execute("__sleep__:3")
        # The worker was killed on timeout, so it is no longer alive.
        assert not p.is_alive()
    finally:
        p.shutdown()


def test_crash_mid_session_raises_engine_crash() -> None:
    p = _proc()
    p.start()
    try:
        with pytest.raises(EngineCrash):
            p.execute("__crash__")
        assert not p.is_alive()
        # A subsequent command on the dead worker also raises crash (not a hang).
        with pytest.raises(EngineCrash):
            p.execute("query-x")
    finally:
        p.shutdown()


def test_clean_shutdown_terminates_worker() -> None:
    p = _proc()
    p.start()
    assert p.is_alive()
    p.shutdown()
    assert not p.is_alive()


def test_execute_before_start_raises_crash() -> None:
    p = _proc()
    with pytest.raises(EngineCrash):
        p.execute("query-x")
