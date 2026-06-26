"""Tool-usability eval harness test (E15-06).

Runs the deterministic eval harness (``evals/run_eval.py``) over the shipped scenarios dataset and
asserts the server's OWN discovery surface (``how_do_i`` / :mod:`inkscape_mcp.intents`) stays
agent-friendly:

* in-scope tool-selection accuracy >= the threshold (regression guard on the intent map / matcher),
* every out-of-scope ask is flagged (never mis-mapped to a tool),
* the dataset is non-trivial and every ``expected_tools`` name is a REAL registered MCP tool.

This both EXERCISES the harness and GUARDS ``intents.py`` / ``how_do_i`` against regressions, per
the epic intent of feeding failures back into E15-02/E14-08.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# The harness lives under evals/ (ruff-checked, not in mypy `files` — mirrors scripts/). Add it to
# the path so the test can import it directly.
_EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(_EVALS_DIR))

from run_eval import DEFAULT_THRESHOLD, load_scenarios, run_eval  # noqa: E402

from inkscape_mcp.server import mcp, register_tools  # noqa: E402

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


def test_overall_tool_selection_accuracy_meets_threshold() -> None:
    report = run_eval()
    acc = report["in_scope"]["accuracy"]
    assert acc >= _ACCURACY_THRESHOLD, (
        f"in-scope tool-selection accuracy {acc:.2%} < {_ACCURACY_THRESHOLD:.0%}; "
        f"failures: {[(r['task'], r['surfaced_tools']) for r in report['failures']]}"
    )


def test_all_out_of_scope_scenarios_flagged() -> None:
    report = run_eval()
    oos = report["out_of_scope"]
    assert oos["total"] >= 3
    assert oos["all_flagged"], (
        "an out-of-scope ask was NOT flagged (got mis-mapped to a tool): "
        f"{[r['task'] for r in report['failures'] if r['out_of_scope']]}"
    )


def test_every_expected_tool_is_a_real_registered_tool() -> None:
    """Guard the dataset against drift: each expected tool must exist on the live MCP surface."""
    register_tools()
    tool_names = {t.name for t in asyncio.run(mcp.list_tools())}
    referenced: set[str] = set()
    for scenario in load_scenarios():
        referenced.update(scenario["expected_tools"])
    missing = sorted(referenced - tool_names)
    assert not missing, f"scenarios reference non-existent tools: {missing}"
