"""run_inkscape: one bounded retry, diagnostic log, scrubbed stderr (run7 F1)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from inkscape_mcp.workspace import subprocess_exec
from inkscape_mcp.workspace.subprocess_exec import ProcessResult


def _res(
    rc: int = 0, duration: float = 0.1, timed_out: bool = False, stderr: str = ""
) -> ProcessResult:
    return ProcessResult(
        args=[], returncode=rc, stdout="", stderr=stderr, duration_s=duration, timed_out=timed_out
    )


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    calls: list[float] = []
    queue: list[ProcessResult] = []
    records: list[logging.LogRecord] = []

    def fake(argv: list[str], timeout_s: float, env: object = None) -> ProcessResult:
        calls.append(timeout_s)
        return queue.pop(0)

    class _H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _H()
    monkeypatch.setattr(subprocess_exec.shutil, "which", lambda _n: "/usr/bin/inkscape")
    monkeypatch.setattr(subprocess_exec, "run_process", fake)
    subprocess_exec._logger.addHandler(handler)  # package logger does not propagate to caplog
    try:
        yield SimpleNamespace(calls=calls, queue=queue, records=records)
    finally:
        subprocess_exec._logger.removeHandler(handler)


def _go(timeout: float = 10.0) -> ProcessResult:
    return subprocess_exec.run_inkscape(["x"], SimpleNamespace(process_timeout_s=timeout))  # type: ignore[arg-type]


def test_crash_then_success_retries_once_and_logs_signal(run: Any) -> None:
    run.queue += [_res(-11), _res(0)]
    assert _go().returncode == 0
    assert len(run.calls) == 2
    rec = run.records[0]
    assert (rec.signal, rec.retry, rec.attempt) == ("SIGSEGV", True, 1)  # type: ignore[attr-defined]


def test_timeout_is_not_retried(run: Any) -> None:
    run.queue += [_res(-1, 10.0, timed_out=True)]
    assert _go().timed_out
    assert len(run.calls) == 1


def test_retry_gets_only_the_remaining_budget(run: Any) -> None:
    run.queue += [_res(1, 4.0), _res(0)]
    _go()
    assert run.calls == [10.0, 6.0]
    run.calls.clear()
    run.queue += [_res(1, 9.5)]
    assert _go().returncode == 1
    assert run.calls == [10.0]


def test_persistent_failure_returns_last_result(run: Any) -> None:
    run.queue += [_res(1), _res(1)]
    assert _go().returncode == 1
    assert len(run.calls) == 2
    assert [r.attempt for r in run.records] == [1, 2]  # type: ignore[attr-defined]


def test_stderr_tail_is_scrubbed_of_host_paths(run: Any) -> None:
    run.queue += [_res(1, stderr="crash in /home/x/ws/a.svg"), _res(0)]
    _go()
    tail = run.records[0].stderr_tail  # type: ignore[attr-defined]
    assert "a.svg" in tail
    assert "/home" not in tail
