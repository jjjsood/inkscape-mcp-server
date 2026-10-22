"""`EngineManager` — serialization, LRU cap, freshness reopen, crash restart, idle reaping.

Exercised against the fake shell (no Inkscape). The fake records each command, so freshness/reopen
behavior is asserted by inspecting what the worker was asked to do.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from inkscape_mcp.config import Settings
from inkscape_mcp.engine.manager import EngineManager
from inkscape_mcp.engine.process import EngineProcess, EngineResponse

FAKE = [sys.executable, str(Path(__file__).parent / "fake_inkscape_shell.py")]


class _RecordingProcess(EngineProcess):
    """Fake-shell worker that logs every command it is sent (shared log per manager)."""

    def __init__(self, log: list[str], settings: Settings) -> None:
        super().__init__(argv=FAKE, settings=settings)
        self._log = log

    def execute(self, command: str, timeout_s: float | None = None) -> EngineResponse:
        self._log.append(command)
        return super().execute(command, timeout_s)


def _manager(
    settings: Settings,
    log: list[str] | None = None,
    spawned: list[EngineProcess] | None = None,
) -> EngineManager:
    log = [] if log is None else log
    spawned = [] if spawned is None else spawned

    def factory() -> EngineProcess:
        proc = _RecordingProcess(log, settings)
        spawned.append(proc)
        return proc

    return EngineManager(settings=settings, process_factory=factory)


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    p = tmp_path / "doc.svg"
    p.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>')
    return p


def test_file_open_once_then_reused_when_unchanged(doc: Path) -> None:
    log: list[str] = []
    mgr = _manager(Settings(), log)
    try:
        assert mgr.run(doc, lambda p: "ok") == "ok"
        mgr.run(doc, lambda p: "ok")
        # Unchanged file on the same warm worker: exactly one file-open, no second worker.
        assert log == [f"file-open:{doc}"]
        assert mgr.active_count == 1
    finally:
        mgr.shutdown_all()


def test_stale_file_triggers_reopen(doc: Path) -> None:
    log: list[str] = []
    mgr = _manager(Settings(), log)
    try:
        mgr.run(doc, lambda p: None)
        # Change size (mtime alone may not tick on coarse filesystems) -> must re-file-open.
        doc.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200"/>')
        mgr.run(doc, lambda p: None)
        assert log == [f"file-open:{doc}", f"file-open:{doc}"]
    finally:
        mgr.shutdown_all()


def test_lru_eviction_at_capacity(tmp_path: Path) -> None:
    mgr = _manager(Settings(engine_max_processes=1))
    a = tmp_path / "a.svg"
    b = tmp_path / "b.svg"
    a.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    b.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    try:
        mgr.run(a, lambda p: None)
        assert mgr.active_count == 1
        mgr.run(b, lambda p: None)
        # Cap is 1: opening b evicts a; still only one worker alive.
        assert mgr.active_count == 1
    finally:
        mgr.shutdown_all()


def test_crash_restart(doc: Path) -> None:
    mgr = _manager(Settings())
    from inkscape_mcp.engine.process import EngineCrash

    def crasher(proc: EngineProcess) -> None:
        proc.execute("__crash__")

    try:
        with pytest.raises(EngineCrash):
            mgr.run(doc, crasher)
        # The dead worker was discarded; a fresh run spawns a new one and succeeds.
        assert mgr.run(doc, lambda p: p.execute("query-x").output_lines) == ["10"]
    finally:
        mgr.shutdown_all()


def test_idle_worker_reaped(doc: Path) -> None:
    spawned: list[EngineProcess] = []
    mgr = _manager(Settings(engine_idle_timeout_s=1.0), spawned=spawned)
    try:
        mgr.run(doc, lambda p: None)
        # Force the worker past its idle timeout, then trigger a reap via another acquire.
        time.sleep(1.05)
        mgr.run(doc, lambda p: None)
        # Reaped (first worker shut down) + respawned; the pool never accumulates.
        assert len(spawned) == 2
        assert not spawned[0].is_alive()
        assert spawned[1].is_alive()
        assert mgr.active_count == 1
    finally:
        mgr.shutdown_all()


def test_commands_serialized_per_worker(doc: Path) -> None:
    mgr = _manager(Settings())
    overlap = {"max": 0, "cur": 0}
    lock = threading.Lock()

    def fn(proc: EngineProcess) -> None:
        with lock:
            overlap["cur"] += 1
            overlap["max"] = max(overlap["max"], overlap["cur"])
        proc.execute("__sleep__:0.1")
        with lock:
            overlap["cur"] -= 1

    try:
        threads = [threading.Thread(target=lambda: mgr.run(doc, fn)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Per-worker lock => never two commands in flight on the same worker at once.
        assert overlap["max"] == 1
    finally:
        mgr.shutdown_all()


def test_shutdown_all_clears_pool(doc: Path) -> None:
    mgr = _manager(Settings())
    mgr.run(doc, lambda p: None)
    assert mgr.active_count == 1
    mgr.shutdown_all()
    assert mgr.active_count == 0
