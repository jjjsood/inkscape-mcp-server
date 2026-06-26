#!/usr/bin/env python3
"""OPTIONAL live-agent eval runner for inkscape-mcp (E15-06a).

Companion to the deterministic harness (:mod:`run_eval`). Where ``run_eval.py`` scores the server's
OWN discovery surface (``how_do_i`` / :mod:`inkscape_mcp.intents`) with NO model, this runner drives
a REAL agent over the MCP server and records the tools it ACTUALLY called per scenario, plus the
turn count and whether the task succeeded.

It consumes the SAME dataset and the SAME scoring + reporting helpers as ``run_eval.py`` — there is
no second copy of ``tool_selection_scenarios.json`` and no forked scorer. The only thing that
differs is the *discovery* step: instead of the deterministic matcher we plug in an agent "driver"
that returns the real tool trace. The shared scorer in :mod:`run_eval` then decides HIT/MISS with
identical rules, so the live numbers are directly comparable to the deterministic ones.

REPORT-ONLY — NOT a CI gate
---------------------------
This runner is **not** part of the deterministic CI gate. It is non-deterministic (needs an LLM and
a live MCP session) and must never block CI. ``tests/test_eval_harness.py`` and
``uv run python evals/run_eval.py`` are entirely unaffected by this module; the only test that
touches this file (``tests/test_live_eval_runner.py``) uses the deterministic FAKE driver and needs
no model. Exit code here is informational (0 unless the runner itself errored) — never a pass/fail
quality bar.

Driver abstraction
------------------
The pluggable boundary is :class:`AgentDriver` (a ``Protocol``): given a scenario ``task`` and the
catalogue of available tool names, it must return an :class:`AgentTrace` — the ordered list of tools
it called, the turn count, and whether it judged the task complete. Two implementations ship here:

* :class:`ReplayDriver` — a deterministic FAKE. It replays a canned ``{task: trace}`` map, or, when
  no canned trace exists, falls back to the deterministic discovery surface so the runner is always
  exercisable. This is what the test uses; no model required.
* :func:`build_real_driver` — the place to plug in a real MCP + LLM client. It is OPTIONAL and
  OFF BY DEFAULT: selected only via ``--driver real`` (or ``INKSCAPE_MCP_EVAL_DRIVER=real``), and it
  raises a clear ``NotImplementedError`` describing the wiring until a real client is supplied. CI
  never selects it.

Usage (run inside ``inkscape-mcp-server/``)::

    uv run python evals/run_live_eval.py               # FAKE replay driver, human summary
    uv run python evals/run_live_eval.py --json        # machine-readable report dict on stdout
    uv run python evals/run_live_eval.py --driver real # real agent (must be wired; off by default)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# Reuse the deterministic harness wholesale: the dataset loader, the per-scenario scorer, and the
# aggregator are all runner-agnostic by design (they take a discovery callable). Importing them here
# is what guarantees the live runner cannot diverge from the deterministic one. Dual-mode import so
# this works both as a flat sibling module (``sys.path`` has ``evals/`` — the tests + the runner's
# own ``__main__``) and as a package-qualified module (``import evals.run_live_eval``).
try:
    from run_eval import _discover, load_scenarios, run_eval
except ModuleNotFoundError:  # pragma: no cover - exercised only via the package-import path
    from evals.run_eval import _discover, load_scenarios, run_eval

#: Env var that selects the driver when ``--driver`` is omitted. Defaults to the FAKE replay driver
#: so nothing here ever reaches for a model unless explicitly asked.
DRIVER_ENV_VAR = "INKSCAPE_MCP_EVAL_DRIVER"

#: Env var pointing at an optional canned-trace JSON file for the replay driver (``{task: [...]}``
#: or ``{task: {"tools": [...], "turns": int, "success": bool}}``). Lets you record a real run once
#: and replay it deterministically in CI/tests without re-invoking a model.
REPLAY_FILE_ENV_VAR = "INKSCAPE_MCP_EVAL_REPLAY"


@dataclass
class AgentTrace:
    """One agent's observable behaviour on a single scenario.

    * ``tools`` — the ordered list of tool names the agent actually CALLED (may contain repeats).
    * ``turns`` — number of agent turns / model round-trips it took.
    * ``success`` — whether the agent (or driver) judged the task complete. ``None`` means "let the
      scorer decide from the trace vs ``expected_tools``" (the usual case for the FAKE driver).
    """

    tools: list[str] = field(default_factory=list)
    turns: int = 0
    success: bool | None = None


@runtime_checkable
class AgentDriver(Protocol):
    """The pluggable agent boundary.

    An implementation drives whatever it likes (a real MCP+LLM loop, a recorded replay, a stub) and
    must return an :class:`AgentTrace`. ``tool_names`` is the catalogue the agent may call, so a
    real driver can advertise exactly the server's tool surface to the model.
    """

    def run(self, task: str, tool_names: list[str]) -> AgentTrace:  # pragma: no cover - protocol
        ...


class ReplayDriver:
    """Deterministic FAKE driver — no model required.

    Resolution order for each task:

    1. an exact canned trace from ``replay`` (a ``{task: AgentTrace|list|dict}`` map), else
    2. the deterministic discovery surface (:func:`run_eval._discover`) as a stand-in trace, so the
       runner is always exercisable end-to-end.

    The turn count for the discovery fallback is modelled as ``1 + len(tools)`` (one planning turn
    plus one call per surfaced tool) — a stable, explainable proxy purely for the FAKE path.
    """

    def __init__(self, replay: dict[str, AgentTrace] | None = None) -> None:
        self._replay = replay or {}

    def run(self, task: str, tool_names: list[str]) -> AgentTrace:
        canned = self._replay.get(task)
        if canned is not None:
            return canned
        surfaced, flagged_oos = _discover(task)
        # If the surface flagged it out-of-scope it "calls" nothing — one turn to refuse.
        if flagged_oos:
            return AgentTrace(tools=[], turns=1, success=True)
        return AgentTrace(tools=list(surfaced), turns=1 + len(surfaced))


def _coerce_trace(raw: Any) -> AgentTrace:
    """Build an :class:`AgentTrace` from canned JSON (a bare tool list or a full dict)."""
    if isinstance(raw, list):
        return AgentTrace(tools=[str(t) for t in raw], turns=1 + len(raw))
    if isinstance(raw, dict):
        tools = [str(t) for t in raw.get("tools", [])]
        turns = int(raw.get("turns", 1 + len(tools)))
        success = raw.get("success")
        return AgentTrace(
            tools=tools, turns=turns, success=None if success is None else bool(success)
        )
    raise ValueError(f"unsupported replay trace shape: {type(raw).__name__}")


def load_replay(path: Path) -> dict[str, AgentTrace]:
    """Load a ``{task: trace}`` replay map from a JSON file for the FAKE driver."""
    data = json.loads(path.read_text(encoding="utf-8"))
    mapping = data["traces"] if isinstance(data, dict) and "traces" in data else data
    return {str(task): _coerce_trace(raw) for task, raw in mapping.items()}


def build_real_driver() -> AgentDriver:
    """Construct the REAL MCP + LLM driver. OPTIONAL, OFF BY DEFAULT, never used in CI.

    This is the single place to wire a live agent. A real implementation would:

    1. boot/connect the inkscape-mcp STDIO server and list its tools,
    2. hand those tools to an LLM agent (Anthropic SDK + MCP, or any MCP client),
    3. run the scenario ``task`` to completion, recording every tool call in order plus the turn
       count, and return an :class:`AgentTrace`.

    It is intentionally unimplemented so that selecting ``--driver real`` without supplying a client
    fails LOUDLY and EARLY rather than silently degrading or hanging in an environment without a
    model. Replace the body with your client wiring (and keep the FAKE driver as the CI/test path).
    """
    raise NotImplementedError(
        "The real live-agent driver is not wired in this environment. It is intentionally "
        "off-by-default and never runs in CI. Implement build_real_driver() with your MCP+LLM "
        "client (see the docstring), or run with the default FAKE replay driver "
        "(`uv run python evals/run_live_eval.py`)."
    )


def select_driver(name: str, replay: dict[str, AgentTrace] | None = None) -> AgentDriver:
    """Pick a driver by name. ``fake``/``replay`` -> :class:`ReplayDriver`; ``real`` -> wired LLM.

    ``real`` requires :func:`build_real_driver` to be wired; it is OFF BY DEFAULT and never CI.
    """
    if name in ("fake", "replay"):
        return ReplayDriver(replay)
    if name == "real":
        return build_real_driver()
    raise ValueError(f"unknown driver {name!r} (expected 'fake', 'replay', or 'real')")


def _tool_catalogue() -> list[str]:
    """Best-effort list of registered MCP tool names to advertise to the driver.

    Imported lazily and defensively: the FAKE driver doesn't need it, and we never want catalogue
    discovery to be the reason this report-only runner fails. Returns ``[]`` if the server can't be
    introspected here.
    """
    try:
        import asyncio

        from inkscape_mcp.server import mcp, register_tools

        register_tools()
        return [t.name for t in asyncio.run(mcp.list_tools())]
    except Exception:
        return []


def run_live_eval(
    driver: AgentDriver,
    scenarios: list[dict[str, Any]] | None = None,
    tool_names: list[str] | None = None,
) -> dict[str, Any]:
    """Drive ``driver`` over every scenario and build a report.

    Each scenario is run through the driver to capture a real :class:`AgentTrace`; that trace is
    then scored with the SHARED scorer from :mod:`run_eval` (HIT iff the trace called an expected
    tool, or correctly called nothing for an out-of-scope ask). The aggregate accuracy / per-group
    / failures structure is identical to the deterministic report, with two extra per-record fields
    — the ordered ``tool_trace`` and the ``turn_count`` — and a ``turns`` summary block.
    """
    scenarios = scenarios if scenarios is not None else load_scenarios()
    catalogue = tool_names if tool_names is not None else []

    # Capture each scenario's live trace once, then expose it to the shared scorer as a discovery
    # callable. We dedupe the ordered call-list into a tool SET for scoring (the scorer asks "was an
    # expected tool among those called?") while preserving the full ordered trace for the report.
    traces: dict[str, AgentTrace] = {}

    def discover(task: str) -> tuple[list[str], bool]:
        trace = driver.run(task, catalogue)
        traces[task] = trace
        seen: list[str] = []
        for tool in trace.tools:
            if tool not in seen:
                seen.append(tool)
        flagged_oos = len(trace.tools) == 0
        return seen, flagged_oos

    report = run_eval(scenarios, discover)

    # Enrich each record with the live-specific signals, and re-derive task success: prefer the
    # driver's explicit verdict, else fall back to the shared scorer's HIT.
    turn_counts: list[int] = []
    for record in report["records"]:
        trace = traces[record["task"]]
        record["tool_trace"] = list(trace.tools)
        record["turn_count"] = trace.turns
        record["task_success"] = (
            bool(record["hit"]) if trace.success is None else bool(trace.success)
        )
        turn_counts.append(trace.turns)

    successes = sum(1 for r in report["records"] if r["task_success"])
    report["task_success"] = {
        "total": len(report["records"]),
        "succeeded": successes,
        "rate": round(successes / len(report["records"]), 4) if report["records"] else 0.0,
    }
    report["turns"] = {
        "total": sum(turn_counts),
        "mean": round(sum(turn_counts) / len(turn_counts), 4) if turn_counts else 0.0,
        "max": max(turn_counts) if turn_counts else 0,
    }
    # Re-derive per-group turn means for the live view (the shared per_group keeps accuracy only).
    grouped: dict[str, list[int]] = defaultdict(list)
    for record in report["records"]:
        grouped[record["group"]].append(record["turn_count"])
    for group, turns in grouped.items():
        report["per_group"][group]["mean_turns"] = (
            round(sum(turns) / len(turns), 4) if turns else 0.0
        )
    return report


def format_live_summary(report: dict[str, Any]) -> str:
    """Render a human-readable summary including per-scenario tool traces + turn counts."""
    lines: list[str] = []
    lines.append("inkscape-mcp LIVE-AGENT eval (report-only — NOT a CI gate)")
    lines.append("=" * 64)
    isc = report["in_scope"]
    oos = report["out_of_scope"]
    ts = report["task_success"]
    turns = report["turns"]
    lines.append(
        f"task success: {ts['rate']:.1%} ({ts['succeeded']}/{ts['total']})  "
        f"in-scope tool-selection: {isc['accuracy']:.1%} ({isc['hits']}/{isc['total']})"
    )
    lines.append(
        f"out-of-scope (no tool called): {oos['flagged']}/{oos['total']} "
        f"({'ALL' if oos['all_flagged'] else 'MISSED some'})"
    )
    lines.append(f"turns: total {turns['total']}  mean {turns['mean']}  max {turns['max']}")
    lines.append("")
    lines.append("per-group (accuracy / mean turns):")
    for group, g in report["per_group"].items():
        lines.append(
            f"  {group:<10} {g['accuracy']:.1%} ({g['hits']}/{g['total']})  "
            f"mean turns {g.get('mean_turns', 0.0)}"
        )
    lines.append("")
    lines.append("per-scenario traces:")
    for r in report["records"]:
        ok = "ok " if r["task_success"] else "MISS"
        lines.append(
            f"  [{ok}] ({r['turn_count']}t) [{r['group']}] {r['task']!r}\n"
            f"        trace={r['tool_trace']} expected={r['expected_tools']}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--driver",
        choices=["fake", "replay", "real"],
        default=os.environ.get(DRIVER_ENV_VAR, "fake"),
        help=(
            "agent driver: 'fake'/'replay' = deterministic FAKE (default, no model); "
            "'real' = live MCP+LLM (off by default, must be wired). Env: " + DRIVER_ENV_VAR
        ),
    )
    parser.add_argument(
        "--replay",
        type=Path,
        default=(
            Path(os.environ[REPLAY_FILE_ENV_VAR]) if REPLAY_FILE_ENV_VAR in os.environ else None
        ),
        help="optional canned-trace JSON file for the FAKE driver (env: "
        + REPLAY_FILE_ENV_VAR
        + ")",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the machine-readable report dict as JSON"
    )
    args = parser.parse_args(argv)

    replay = load_replay(args.replay) if args.replay else None
    driver = select_driver(args.driver, replay)
    report = run_live_eval(driver, tool_names=_tool_catalogue())

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_live_summary(report))
        # Report-only: this line is informational, never a CI pass/fail gate.
        print("\nNOTE: live-agent eval is REPORT-ONLY and not part of the CI gate.", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
