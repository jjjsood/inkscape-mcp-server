"""Live-agent eval runner test.

Exercises the OPTIONAL live-agent runner (``evals/run_live_eval.py``) with the deterministic FAKE
replay driver over the SHARED scenario dataset — no real model required. Asserts the runner emits a
per-scenario report carrying the ordered tool trace + turn count + task success, and that it reuses
(does not fork) the deterministic harness's dataset and scorer.

This runner is REPORT-ONLY and not a CI gate; these tests only check structure/shape, never a
quality threshold.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The eval runners live under evals/ (ruff-checked, not in mypy `files`). Add it to the path so the
# test can import them directly, mirroring tests/test_eval_harness.py.
_EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(_EVALS_DIR))

from run_eval import load_scenarios  # noqa: E402
from run_live_eval import (  # noqa: E402
    ReplayDriver,
    run_live_eval,
)


def test_fake_driver_produces_per_scenario_trace_and_turns() -> None:
    report = run_live_eval(ReplayDriver())

    scenarios = load_scenarios()
    assert report["total"] == len(scenarios)
    assert len(report["records"]) == len(scenarios)

    # Every record carries the live-specific signals: an ordered tool trace, a turn count, and a
    # task-success verdict.
    for record in report["records"]:
        assert "tool_trace" in record
        assert isinstance(record["tool_trace"], list)
        assert all(isinstance(t, str) for t in record["tool_trace"])
        assert isinstance(record["turn_count"], int)
        assert record["turn_count"] >= 1
        assert isinstance(record["task_success"], bool)

    # The aggregate summary blocks the live runner adds on top of the shared report.
    assert set(report["task_success"]) == {"total", "succeeded", "rate"}
    assert set(report["turns"]) == {"total", "mean", "max"}
    assert report["task_success"]["total"] == len(scenarios)


def test_fake_driver_shares_dataset_and_scorer_with_deterministic_harness() -> None:
    # With the FAKE driver falling back to the deterministic discovery surface, the live runner must
    # reproduce the SAME scoring as the deterministic harness (proving a shared scorer, not a fork).
    from run_eval import run_eval

    det = run_eval()
    live = run_live_eval(ReplayDriver())

    assert live["in_scope"]["accuracy"] == det["in_scope"]["accuracy"]
    assert live["out_of_scope"]["all_flagged"] == det["out_of_scope"]["all_flagged"]
    assert live["overall_accuracy"] == det["overall_accuracy"]


def test_out_of_scope_task_records_empty_trace() -> None:
    oos = next(s for s in load_scenarios() if s["out_of_scope"])
    report = run_live_eval(ReplayDriver())
    record = next(r for r in report["records"] if r["task"] == oos["task"])

    # The FAKE driver "calls nothing" for an out-of-scope ask; that is the correct behaviour and a
    # HIT under the shared scorer.
    assert record["tool_trace"] == []
    assert record["task_success"] is True
