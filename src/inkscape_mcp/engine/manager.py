"""Warm-shell worker pool (ADR-007).

`EngineManager` maps a document's working-copy path to a long-lived :class:`EngineProcess` that has
that document ``file-open``-ed, so engine ops run against a warm, stateful worker. It owns its
lifecycle:

- **Per-worker command serialization.** The shell is single-threaded, so each worker keeps a lock
  and the manager runs at most one command on it at a time.
- **Freshness.** Before handing a worker to a caller it re-``file-open``s the working copy iff the
  on-disk file changed (mtime/size) since the worker last opened it, OR the caller forces a reopen
  (mutating ops always do — they reload disk so the warm worker's in-memory document can never
  diverge from the on-disk working copy; "reconcile in-process ↔ disk").
- **Bounded pool + LRU eviction.** At most ``engine_max_processes`` workers; the least-recently-used
  is shut down to make room. Workers idle beyond ``engine_idle_timeout_s`` are reaped on access.
- **Crash → restart.** A dead worker is replaced and the document re-opened transparently.

The manager raises only :class:`EngineError` subclasses; callers wrap every engine op so any fault
falls back to the per-call CLI (correctness can never regress).

SECURITY (sec.12): the only path reaching ``file-open`` is the caller-supplied working-copy path,
which is the registry's sandbox-validated ``working_path`` (never raw client input). The worker argv
is fixed and ``shell=False``.
"""

from __future__ import annotations

import atexit
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.engine.process import EngineCrash, EngineError, EngineProcess
from inkscape_mcp.logging_setup import get_logger

_logger = get_logger("engine.manager")

T = TypeVar("T")


class _Worker:
    """One pooled worker plus its serialization lock."""

    __slots__ = ("lock", "process")

    def __init__(self, process: EngineProcess) -> None:
        self.process = process
        self.lock = threading.Lock()


class EngineManager:
    """A bounded pool of warm `inkscape --shell` workers keyed by working-copy path."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        process_factory: Callable[[], EngineProcess] | None = None,
    ) -> None:
        self._settings = settings if settings is not None else get_settings()
        #: Inject a fake-shell factory in tests; default spawns a real `inkscape --shell`.
        self._factory = process_factory or (lambda: EngineProcess(settings=self._settings))
        self._workers: OrderedDict[str, _Worker] = OrderedDict()
        self._pool_lock = threading.Lock()

    def run(
        self,
        working_path: str | Path,
        fn: Callable[[EngineProcess], T],
        *,
        force_reopen: bool = False,
    ) -> T:
        """Run `fn` against the warm worker for `working_path`, serialized per worker.

        Ensures a live worker exists (spawning/restarting as needed), re-``file-open``s the working
        copy when it is stale on disk or `force_reopen` is set, then calls `fn(process)` while
        holding that worker's lock. Returns `fn`'s value. Raises :class:`EngineError` on any
        engine fault (the caller falls back to the per-call CLI).
        """
        key = str(Path(working_path))
        worker = self._acquire_worker(key)
        with worker.lock:
            self._ensure_open(worker, key, force_reopen=force_reopen)
            try:
                return fn(worker.process)
            except EngineCrash:
                # A mid-command crash invalidates the worker; drop it so the next call respawns.
                self._discard(key, worker)
                raise

    # --- pool management ----------------------------------------------------

    def _acquire_worker(self, key: str) -> _Worker:
        """Return a live worker for `key`, reaping idle workers and enforcing the LRU cap."""
        with self._pool_lock:
            self._reap_idle_locked()
            worker = self._workers.get(key)
            if worker is not None and worker.process.is_alive():
                self._workers.move_to_end(key)
                return worker
            if worker is not None:
                # Dead worker: drop it (crash-restart path).
                self._shutdown_worker(worker)
                del self._workers[key]
            self._evict_to_capacity_locked(reserve=1)
            process = self._factory()
            process.start()
            worker = _Worker(process)
            self._workers[key] = worker
            self._workers.move_to_end(key)
            return worker

    def _ensure_open(self, worker: _Worker, key: str, *, force_reopen: bool) -> None:
        """`file-open` the working copy in `worker` if not yet open, stale on disk, or forced."""
        process = worker.process
        stat = self._stat(key)
        needs_open = (
            force_reopen
            or process.opened_path != key
            or stat is None
            or process.opened_mtime_ns != stat[0]
            or process.opened_size != stat[1]
        )
        if not needs_open:
            return
        process.execute(f"file-open:{key}")
        process.opened_path = key
        if stat is not None:
            process.opened_mtime_ns, process.opened_size = stat

    @staticmethod
    def _stat(key: str) -> tuple[int, int] | None:
        try:
            st = Path(key).stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    def _reap_idle_locked(self) -> None:
        timeout = self._settings.engine_idle_timeout_s
        stale = [
            k
            for k, w in self._workers.items()
            if not w.process.is_alive() or w.process.idle_seconds() > timeout
        ]
        for k in stale:
            self._shutdown_worker(self._workers[k])
            del self._workers[k]

    def _evict_to_capacity_locked(self, *, reserve: int = 0) -> None:
        cap = self._settings.engine_max_processes
        while len(self._workers) + reserve > cap and self._workers:
            _key, worker = self._workers.popitem(last=False)  # LRU = oldest
            self._shutdown_worker(worker)

    def _discard(self, key: str, worker: _Worker) -> None:
        with self._pool_lock:
            current = self._workers.get(key)
            if current is worker:
                del self._workers[key]
        self._shutdown_worker(worker)

    @staticmethod
    def _shutdown_worker(worker: _Worker) -> None:
        try:
            worker.process.shutdown()
        except EngineError:  # pragma: no cover - shutdown is best-effort
            _logger.warning("engine worker shutdown failed")

    def shutdown_all(self) -> None:
        """Shut down every pooled worker (server shutdown / test teardown)."""
        with self._pool_lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            self._shutdown_worker(worker)

    @property
    def active_count(self) -> int:
        """Number of pooled workers (diagnostic/test helper)."""
        with self._pool_lock:
            return len(self._workers)


_manager_singleton: EngineManager | None = None
_singleton_lock = threading.Lock()


def get_engine_manager() -> EngineManager:
    """Return the process-wide :class:`EngineManager` singleton (reset for tests via `reset`)."""
    global _manager_singleton
    with _singleton_lock:
        if _manager_singleton is None:
            _manager_singleton = EngineManager()
            # Reap any warm workers on interpreter exit so no `inkscape --shell` child lingers.
            atexit.register(reset_engine_manager)
        return _manager_singleton


def reset_engine_manager() -> None:
    """Shut down and drop the singleton manager (test helper / server shutdown)."""
    global _manager_singleton
    with _singleton_lock:
        if _manager_singleton is not None:
            _manager_singleton.shutdown_all()
            _manager_singleton = None


__all__ = [
    "EngineManager",
    "get_engine_manager",
    "reset_engine_manager",
]
