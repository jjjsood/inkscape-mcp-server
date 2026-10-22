"""Tool-usability eval harness test.

Runs the deterministic eval harness (``evals/run_eval.py``) over the shipped scenarios dataset and
asserts the server's OWN discovery surface (``help(goal=...)`` / :mod:`inkscape_mcp.intents`)
stays agent-friendly:

* in-scope tool-selection accuracy >= the threshold (regression guard on the intent map / matcher),
* every out-of-scope ask is flagged (never mis-mapped to a tool),
* the dataset is non-trivial and every ``expected_tools`` entry is a REAL ``tool:op`` on the
  registered MCP surface.

This both EXERCISES the harness and GUARDS ``intents.py`` / ``help(goal=...)`` against regressions,
per the intent of feeding eval failures back into the intent map and discovery surface.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The harness lives under evals/ (ruff-checked, not in mypy `files` — mirrors scripts/). Add it to
# the path so the test can import it directly.
_EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(_EVALS_DIR))

from run_eval import DEFAULT_THRESHOLD, _covers, load_scenarios, run_eval  # noqa: E402

from tests.tool_support import target_ops, tool_schemas  # noqa: E402

#: The bar the discovery surface must clear. Realistic per the epic (>= 0.8); the harness currently
#: scores 1.0 in-scope, so this leaves headroom while still catching real regressions.
_ACCURACY_THRESHOLD = DEFAULT_THRESHOLD


def test_dataset_is_non_trivial_and_well_formed() -> None:
    scenarios = load_scenarios()
    assert len(scenarios) >= 20, "expected ~20-40 scenarios"
    # At least a few out-of-scope asks are present so the refusal path is exercised.
    assert sum(1 for s in scenarios if s["out_of_scope"]) >= 3
    groups = {s["group"] for s in scenarios}
    assert {"Create", "Edit", "Inspect", "Export", "Live"} <= groups


@pytest.fixture(scope="module")
def report() -> dict:
    return run_eval()


def test_overall_tool_selection_accuracy_meets_threshold(report: dict) -> None:
    acc = report["in_scope"]["accuracy"]
    assert acc >= _ACCURACY_THRESHOLD, (
        f"in-scope tool-selection accuracy {acc:.2%} < {_ACCURACY_THRESHOLD:.0%}; "
        f"failures: {[(r['task'], r['surfaced_tools']) for r in report['failures']]}"
    )


def test_all_out_of_scope_scenarios_flagged(report: dict) -> None:
    oos = report["out_of_scope"]
    assert oos["total"] >= 3
    assert oos["all_flagged"], (
        "an out-of-scope ask was NOT flagged (got mis-mapped to a tool): "
        f"{[r['task'] for r in report['failures'] if r['out_of_scope']]}"
    )


def test_every_expected_tool_is_a_real_tool_and_op(tool_app) -> None:
    """Guard the dataset against drift: each expected `tool:op` must exist on the live surface."""
    schemas = tool_schemas(tool_app)
    bad: list[str] = []
    for scenario in load_scenarios():
        for expected in scenario["expected_tools"]:
            tool, _, op = expected.partition(":")
            if tool not in schemas:
                bad.append(f"{expected}: unknown tool")
            elif op and op not in target_ops(tool, schemas):
                bad.append(f"{expected}: unknown op")
    assert not bad, f"scenarios reference non-existent tools/ops: {bad}"


def test_bare_tool_names_cover_any_op_but_ops_match_exactly() -> None:
    assert _covers("edit", ["edit:fill"]) and _covers("edit:fill", ["edit:fill"])
    assert not _covers("edit:fill", ["edit:stroke"]) and not _covers("edit", ["editor:fill"])
    assert not _covers("export", [])
