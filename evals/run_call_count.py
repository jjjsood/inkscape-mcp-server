#!/usr/bin/env python3
"""Call-count eval: how many MCP calls (and response bytes) a canonical task takes.

Drives scripted scenarios through the in-memory FastMCP `Client` against the registered server and
records, per scenario, the number of tool calls, the total response bytes the model would receive,
and how many calls failed. Used to compare against the pre-rework surface (scenario A cost 5 calls,
B 6, C 4 there).

Without an Inkscape binary the render/export steps fail; they are still COUNTED (the call a real
agent would make) and flagged `failed` so the report stays honest.

Usage::

    uv run python evals/run_call_count.py                  # human table
    uv run python evals/run_call_count.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SAMPLE_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="120" viewBox="0 0 200 120">
  <rect id="bg" x="0" y="0" width="200" height="120" fill="#ff0000"/>
  <circle id="dot" cx="100" cy="60" r="30" fill="#ff0000"/>
  <text id="label" x="20" y="100" fill="#222222">Hello</text>
</svg>
"""


@dataclass
class Step:
    tool: str
    args: Callable[[dict[str, Any]], dict[str, Any]]
    #: Records the call's structured result into the shared context for later steps.
    capture: Callable[[dict[str, Any], Any], None] | None = None


@dataclass
class Scenario:
    key: str
    title: str
    steps: list[Step] = field(default_factory=list)


def _doc_id(ctx: dict[str, Any], result: Any) -> None:
    data = result.structured_content or json.loads(result.content[0].text)
    ctx["doc_id"] = data["doc_id"]


def scenarios_for(workdir: Path) -> list[Scenario]:
    """The same tasks on the result-oriented surface: one call per outcome, not per element."""
    (workdir / "logo.svg").write_text(SAMPLE_SVG, encoding="utf-8")
    return [
        Scenario(
            "A",
            "Draw a logo, export PNG + SVG",
            [
                Step(
                    "open",
                    lambda c: {
                        "canvas": {"width": 200, "height": 120},
                        "ops": [
                            {
                                "op": "create_rect",
                                "x": 0,
                                "y": 0,
                                "width": 200,
                                "height": 120,
                                "fill": "#3366cc",
                                "id": "bg",
                            },
                            {"op": "create_circle", "cx": 100, "cy": 60, "r": 30, "fill": "#fff"},
                            {"op": "create_text", "x": 20, "y": 100, "text": "Logo"},
                        ],
                        "render": "inline",
                    },
                    _doc_id,
                ),
                Step(
                    "export",
                    lambda c: {
                        "doc_id": c["doc_id"],
                        "targets": [
                            {"kind": "file", "format": "png"},
                            {"kind": "file", "format": "svg"},
                        ],
                    },
                ),
            ],
        ),
        Scenario(
            "B",
            "Recolor an existing file, export for web",
            [
                Step("open", lambda c: {"path": str(workdir / "logo.svg")}, _doc_id),
                Step(
                    "edit",
                    lambda c: {
                        "doc_id": c["doc_id"],
                        "ops": [
                            {"op": "palette", "mapping": {"#ff0000": "#3366cc"}},
                            {"op": "optimize_web"},
                        ],
                        "render": "inline",
                    },
                ),
                Step(
                    "export",
                    lambda c: {
                        "doc_id": c["doc_id"],
                        "targets": [{"kind": "profile", "name": "web"}],
                    },
                ),
            ],
        ),
    ]


#: Scenario C (live: inspect selection, recolor, render) needs the in-memory live transport from
#: tests/conftest.py, so it is counted statically: `live` observes (and connects), `live_edit`
#: recolors and renders = 2 calls (the pre-rework surface needed 4).
STATIC = {"C": {"title": "Live: inspect selection, recolor, render", "calls": 2}}


def response_bytes(result: Any) -> int:
    """Bytes the model receives for one call: text blocks, structuredContent, image payloads."""
    total = 0
    for block in result.content or []:
        text = getattr(block, "text", None)
        data = getattr(block, "data", None)
        total += len(text) if text else len(data) if data else 0
    if result.structured_content:
        total += len(json.dumps(result.structured_content, separators=(",", ":")))
    return total


async def run(workdir: Path) -> list[dict[str, Any]]:
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from inkscape_mcp.server import mcp as app
    from inkscape_mcp.server import register_tools

    register_tools()
    scenarios = scenarios_for(workdir)

    report: list[dict[str, Any]] = []
    async with Client(app) as client:
        for scenario in scenarios:
            ctx: dict[str, Any] = {}
            calls = failed = total_bytes = 0
            for step in scenario.steps:
                calls += 1
                try:
                    result = await client.call_tool(step.tool, step.args(ctx))
                except (ToolError, KeyError) as exc:
                    failed += 1
                    total_bytes += len(str(exc))
                    if step.capture:  # a failed capture step ends the scenario
                        break
                    continue
                total_bytes += response_bytes(result)
                if step.capture:
                    step.capture(ctx, result)
            report.append(
                {
                    "scenario": scenario.key,
                    "title": scenario.title,
                    "calls": calls,
                    "failed": failed,
                    "response_bytes": total_bytes,
                }
            )
    for key, info in STATIC.items():
        report.append(
            {
                "scenario": key,
                "title": info["title"],
                "calls": info["calls"],
                "failed": None,
                "response_bytes": None,
                "static": True,
            }
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    workdir = Path(
        os.environ.get("INKSCAPE_MCP_WORKSPACE_ROOTS") or tempfile.mkdtemp(prefix="im-cc-")
    )
    os.environ["INKSCAPE_MCP_WORKSPACE_ROOTS"] = str(workdir)

    report = asyncio.run(run(workdir))
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    for row in report:
        extra = (
            " (static)"
            if row.get("static")
            else (f", failed {row['failed']}, {row['response_bytes']:,} B")
        )
        print(f"  {row['scenario']}: {row['calls']} calls{extra}  — {row['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
