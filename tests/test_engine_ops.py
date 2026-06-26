"""shell-command composition (`engine_export_document` / `engine_run_actions`).

Run against the fake shell via an injected `EngineManager`, plus the config-gate (`engine_mode`).
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

from inkscape_mcp.config import (
    ENGINE_MODE_PER_CALL,
    ENGINE_MODE_SHELL,
    ENV_ENGINE_MODE,
    Settings,
    get_settings,
)
from inkscape_mcp.engine import ops as ops_mod
from inkscape_mcp.engine.manager import EngineManager
from inkscape_mcp.engine.ops import (
    engine_export_document,
    engine_mode_is_shell,
    engine_run_actions,
)
from inkscape_mcp.engine.process import EngineProcess, EngineUnavailable

FAKE = [sys.executable, str(Path(__file__).parent / "fake_inkscape_shell.py")]


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    p = tmp_path / "doc.svg"
    p.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 60"/>')
    return p


@pytest.fixture
def fake_manager(monkeypatch: pytest.MonkeyPatch) -> EngineManager:
    settings = Settings()
    mgr = EngineManager(
        settings=settings,
        process_factory=lambda: EngineProcess(argv=FAKE, settings=settings),
    )
    monkeypatch.setattr(ops_mod, "get_engine_manager", lambda: mgr)
    return mgr


def _png_dims(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", head[16:24])


def test_engine_export_png_writes_artifact(
    doc: Path, tmp_path: Path, fake_manager: EngineManager
) -> None:
    out = tmp_path / "out.png"
    try:
        engine_export_document(doc, out, fmt="png", width_px=200, settings=Settings())
        assert out.exists()
        assert _png_dims(out) == (200, 120)
    finally:
        fake_manager.shutdown_all()


def test_engine_export_svg_writes_artifact(
    doc: Path, tmp_path: Path, fake_manager: EngineManager
) -> None:
    out = tmp_path / "out.svg"
    try:
        engine_export_document(doc, out, fmt="svg", width_px=None, settings=Settings())
        assert out.exists()
        assert b"<svg" in out.read_bytes()
    finally:
        fake_manager.shutdown_all()


def test_engine_run_actions_returns_safe_svg_bytes(doc: Path, fake_manager: EngineManager) -> None:
    try:
        data = engine_run_actions(doc, "select-by-id:r1;path-union", settings=Settings())
        assert data.strip().startswith(b"<svg") or b"<svg" in data
    finally:
        fake_manager.shutdown_all()


def test_unsafe_output_path_refused(doc: Path, tmp_path: Path, fake_manager: EngineManager) -> None:
    # A path carrying the action separator ';' is refused so the caller falls back to per-call.
    bad = tmp_path / "a;b.png"
    with pytest.raises(EngineUnavailable):
        engine_export_document(doc, bad, fmt="png", width_px=None, settings=Settings())


def test_unsafe_working_path_refused(tmp_path: Path, fake_manager: EngineManager) -> None:
    bad = tmp_path / "a;b.svg"
    bad.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    with pytest.raises(EngineUnavailable):
        engine_run_actions(bad, "select-all;path-union", settings=Settings())


def test_engine_mode_is_shell_reads_settings() -> None:
    assert engine_mode_is_shell(Settings(engine_mode=ENGINE_MODE_SHELL)) is True
    assert engine_mode_is_shell(Settings(engine_mode=ENGINE_MODE_PER_CALL)) is False


def test_engine_mode_gate_defaults_and_floors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_ENGINE_MODE, raising=False)
    get_settings.cache_clear()
    assert get_settings().engine_mode == ENGINE_MODE_PER_CALL  # default

    monkeypatch.setenv(ENV_ENGINE_MODE, "shell")
    get_settings.cache_clear()
    assert get_settings().engine_mode == ENGINE_MODE_SHELL

    monkeypatch.setenv(ENV_ENGINE_MODE, "GARBAGE")
    get_settings.cache_clear()
    assert get_settings().engine_mode == ENGINE_MODE_PER_CALL  # floors to the safe baseline
    get_settings.cache_clear()
