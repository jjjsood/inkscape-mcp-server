#!/usr/bin/env python3
"""Tool-usability eval harness for inkscape-mcp (E15-06).

Makes "agent-friendly" MEASURABLE without a live LLM. For every scenario in
``tool_selection_scenarios.json`` it drives the server's OWN deterministic discovery layer — the
same out-of-scope detector + keyword matcher that backs the ``how_do_i`` tool
(:mod:`inkscape_mcp.intents`) — and scores whether the surfaced tool(s) include the expected
tool(s). It then reports per-group and overall tool-selection accuracy plus how many out-of-scope
asks were correctly flagged.

Why no LLM: the thing under test is the server's discoverability surface (the curated intent map +
matcher that E14-08 shipped and E15-03 catalogued), not a model. Evaluating it deterministically
makes the harness CI-runnable and a regression guard (``tests/test_eval_harness.py``) — a
best-practice delta over the survey: none of the §6 compared repos evaluate their tool surface.

Scoring per scenario:
  * in-scope  → HIT when at least one ``expected_tools`` entry appears among the tools the matcher
    returned (matcher returns up to N entries; we union their tool lists), AND the ask was not
    wrongly flagged out-of-scope.
  * out-of-scope (``out_of_scope: true``) → HIT when the detector flags it out-of-scope (so the
    server refuses to map it to a tool). Tracked separately as out-of-scope precision/recall too.

Swapping in a real agent later: keep the JSON schema (``task`` / ``expected_tools`` / ``group`` /
``out_of_scope``); replace :func:`_discover` with a function that drives a live MCP agent and
returns the tools it actually CALLED (and, optionally, an out-of-scope refusal). The scoring +
reporting below are runner-agnostic — only :func:`_discover` couples to the deterministic surface.

Usage (run inside ``inkscape-mcp-server/``)::

    uv run python evals/run_eval.py            # human summary, exit 0/1 vs the default threshold
    uv run python evals/run_eval.py --json     # machine-readable report dict on stdout

Exit code is 0 when overall tool-selection accuracy meets the threshold AND every out-of-scope ask
was flagged; non-zero otherwise (so it can gate CI directly as well as via pytest).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Import the SAME deterministic discovery layer that backs the `how_do_i` tool. Pure module — no MCP
# decorators, no LLM, no side effects on import — so the harness needs no server boot.
from inkscape_mcp.intents import detect_out_of_scope, match_intents

#: A discovery callable: maps a task string to ``(surfaced_tools, flagged_out_of_scope)``. The
#: deterministic harness passes :func:`_discover`; the live-agent runner (``run_live_eval.py``)
#: passes a function that drives a real agent and returns the tools it actually CALLED. Keeping the
#: scorer parametrised over this boundary lets both runners SHARE the scoring + reporting below
#: without duplicating them — exactly the "runner-agnostic" split promised in the module docstring.
DiscoverFn = Callable[[str], tuple[list[str], bool]]

#: Default accuracy bar for the in-scope tool-selection score. The pytest guard asserts the same.
DEFAULT_THRESHOLD = 0.8

#: The scenarios dataset lives next to this file.
SCENARIOS_PATH = Path(__file__).with_name("tool_selection_scenarios.json")


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[dict[str, Any]]:
    """Load and lightly validate the scenarios list from the JSON dataset.

    Each scenario must carry a ``task`` (str), ``expected_tools`` (list[str]), and ``group`` (str);
    ``out_of_scope`` (bool) is optional and defaults to False.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    scenarios = data["scenarios"] if isinstance(data, dict) else data
    out: list[dict[str, Any]] = []
    for raw in scenarios:
        out.append(
            {
                "task": str(raw["task"]),
                "expected_tools": [str(t) for t in raw.get("expected_tools", [])],
                "group": str(raw.get("group", "Unknown")),
                "out_of_scope": bool(raw.get("out_of_scope", False)),
            }
        )
    return out


def _discover(task: str) -> tuple[list[str], bool]:
    """Drive the deterministic discovery surface for one task.

    Mirrors ``how_do_i``: out-of-scope is detected FIRST; otherwise the keyword matcher returns up
    to a few guidance entries and we union their tool lists. Returns ``(tools, out_of_scope)``.
    """
    if detect_out_of_scope(task) is not None:
        return [], True
    tools: list[str] = []
    for match in match_intents(task):
        for tool in match.tools:
            if tool not in tools:
                tools.append(tool)
    return tools, False


def score_scenario(scenario: dict[str, Any], discover: DiscoverFn = _discover) -> dict[str, Any]:
    """Run one scenario through a discovery callable and decide HIT/MISS.

    ``discover`` defaults to the deterministic :func:`_discover` (the ``how_do_i`` surface), so the
    existing harness behaves identically. The live-agent runner passes its own callable — the
    scoring rule is the same either way, which is what keeps the two runners directly comparable.

    Returns a per-scenario record with the surfaced tools, the verdict, and (for in-scope asks)
    which expected tools were/weren't covered — useful for feeding failures back into the map.
    """
    surfaced, flagged_oos = discover(scenario["task"])
    expected = scenario["expected_tools"]

    if scenario["out_of_scope"]:
        hit = flagged_oos and not surfaced
    else:
        # HIT iff at least one expected tool is among the surfaced tools, and we did NOT wrongly
        # flag an in-scope ask as out-of-scope.
        hit = (not flagged_oos) and any(tool in surfaced for tool in expected)

    return {
        "task": scenario["task"],
        "group": scenario["group"],
        "out_of_scope": scenario["out_of_scope"],
        "expected_tools": expected,
        "surfaced_tools": surfaced,
        "flagged_out_of_scope": flagged_oos,
        "hit": hit,
        "missing_tools": [t for t in expected if t not in surfaced],
    }


def run_eval(
    scenarios: list[dict[str, Any]] | None = None, discover: DiscoverFn = _discover
) -> dict[str, Any]:
    """Score every scenario and aggregate into a report dict (no printing).

    ``discover`` defaults to the deterministic :func:`_discover`, so callers that omit it (the
    existing CLI, ``tests/test_eval_harness.py``) keep the exact same behaviour. The live-agent
    runner reuses this aggregation by passing its own discovery callable.

    Report shape::

        {
          "total": int, "hits": int, "overall_accuracy": float,
          "in_scope": {"total", "hits", "accuracy"},
          "out_of_scope": {"total", "flagged", "all_flagged": bool},
          "per_group": {group: {"total", "hits", "accuracy"}},
          "failures": [ <scenario record where hit is False> ],
          "records": [ <every scenario record> ],
        }
    """
    scenarios = scenarios if scenarios is not None else load_scenarios()
    records = [score_scenario(s, discover) for s in scenarios]

    in_scope = [r for r in records if not r["out_of_scope"]]
    out_of_scope = [r for r in records if r["out_of_scope"]]

    in_hits = sum(1 for r in in_scope if r["hit"])
    oos_flagged = sum(1 for r in out_of_scope if r["flagged_out_of_scope"])
    total_hits = sum(1 for r in records if r["hit"])

    per_group: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        grouped[r["group"]].append(r)
    for group, recs in sorted(grouped.items()):
        g_hits = sum(1 for r in recs if r["hit"])
        per_group[group] = {
            "total": len(recs),
            "hits": g_hits,
            "accuracy": round(g_hits / len(recs), 4) if recs else 0.0,
        }

    return {
        "total": len(records),
        "hits": total_hits,
        "overall_accuracy": round(total_hits / len(records), 4) if records else 0.0,
        "in_scope": {
            "total": len(in_scope),
            "hits": in_hits,
            # The headline "tool-selection accuracy" metric (in-scope only).
            "accuracy": round(in_hits / len(in_scope), 4) if in_scope else 0.0,
        },
        "out_of_scope": {
            "total": len(out_of_scope),
            "flagged": oos_flagged,
            "all_flagged": oos_flagged == len(out_of_scope),
        },
        "per_group": per_group,
        "failures": [r for r in records if not r["hit"]],
        "records": records,
    }


def format_summary(report: dict[str, Any], threshold: float) -> str:
    """Render a human-readable summary of a report dict."""
    lines: list[str] = []
    lines.append("inkscape-mcp tool-usability eval (deterministic discovery surface)")
    lines.append("=" * 64)
    isc = report["in_scope"]
    oos = report["out_of_scope"]
    lines.append(
        f"in-scope tool-selection accuracy: {isc['accuracy']:.1%} "
        f"({isc['hits']}/{isc['total']})  threshold {threshold:.0%}"
    )
    lines.append(
        f"out-of-scope flagged: {oos['flagged']}/{oos['total']} "
        f"({'ALL flagged' if oos['all_flagged'] else 'MISSED some'})"
    )
    lines.append(
        f"overall accuracy: {report['overall_accuracy']:.1%} ({report['hits']}/{report['total']})"
    )
    lines.append("")
    lines.append("per-group:")
    for group, g in report["per_group"].items():
        lines.append(f"  {group:<10} {g['accuracy']:.1%}  ({g['hits']}/{g['total']})")
    if report["failures"]:
        lines.append("")
        lines.append("failures:")
        for r in report["failures"]:
            detail = (
                "wrongly flagged out-of-scope"
                if r["flagged_out_of_scope"] and not r["out_of_scope"]
                else f"surfaced={r['surfaced_tools']} missing={r['missing_tools']}"
            )
            lines.append(f"  [{r['group']}] {r['task']!r} -> {detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"minimum in-scope tool-selection accuracy to pass (default {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the machine-readable report dict as JSON"
    )
    args = parser.parse_args(argv)

    report = run_eval()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_summary(report, args.threshold))

    passed = (
        report["in_scope"]["accuracy"] >= args.threshold and report["out_of_scope"]["all_flagged"]
    )
    if not args.json:
        print("\nRESULT:", "PASS" if passed else "FAIL", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
