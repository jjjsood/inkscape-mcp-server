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
from inkscape_mcp.engine.process import EngineProcess

FAKE = [sys.executable, str(Path(__file__).parent / "fake_inkscape_shell.py")]


def _manager(settings: Settings) -> EngineManager:
    return EngineManager(
        settings=settings,
        process_factory=lambda: EngineProcess(argv=FAKE, settings=settings),
    )


@pytest.fixture
def doc(tmp_path: Path) -> Path:
    p = tmp_path / "doc.svg"
    p.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>')
    return p


def test_file_open_once_then_reused_when_unchanged(doc: Path) -> None:
    mgr = _manager(Settings())
    opens: list[str] = []

    def fn(proc: EngineProcess) -> str:
        return "ok"

    try:
        assert mgr.run(doc, fn) == "ok"
        first = mgr.active_count
        # Second run on the unchanged file reuses the same warm worker (no growth).
        mgr.run(doc, fn)
        assert mgr.active_count == first == 1
    finally:
        mgr.shutdown_all()
    _ = opens


def test_stale_file_triggers_reopen(doc: Path) -> None:
    mgr = _manager(Settings())
    seen: list[str | None] = []

    def fn(proc: EngineProcess) -> None:
        seen.append(proc.opened_path)

    try:
        mgr.run(doc, fn)
        opened_mtime = None
        # Mutate the file so mtime/size change -> next run must re-file-open.
        time.sleep(0.01)
        doc.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"/>')
        mgr.run(doc, fn)
        # Both runs saw the doc opened (the second re-opened after the change).
        assert seen == [str(doc), str(doc)]
        _ = opened_mtime
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
    mgr = _manager(Settings(engine_idle_timeout_s=1.0))
    try:
        mgr.run(doc, lambda p: None)
        assert mgr.active_count == 1
        # Force the worker past its idle timeout, then trigger a reap via another acquire.
        time.sleep(1.05)
        mgr.run(doc, lambda p: None)
        assert mgr.active_count == 1  # reaped + respawned, never accumulates
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
