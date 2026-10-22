#!/usr/bin/env python3
"""Measure the MCP tool surface: tool count and `tools/list` wire size.

Registers the server with the CURRENT environment flags, lists the tools the way a client
sees them, and reports bytes per tool plus totals (description vs. input/output schema). Used to
track the payload budget across the surface rework (docs/rework-plan.md).

Usage::

    uv run python scripts/measure_surface.py            # human summary (registered server)
    uv run python scripts/measure_surface.py --json     # machine-readable
    uv run python scripts/measure_surface.py --top 15   # more rows in the per-tool table
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile


def measure() -> dict:
    from inkscape_mcp.server import mcp, register_tools

    register_tools()
    tools = asyncio.run(mcp.list_tools())
    rows = []
    for tool in tools:
        wire = tool.to_mcp_tool().model_dump(exclude_none=True, by_alias=True)
        rows.append(
            {
                "name": wire["name"],
                "total": len(json.dumps(wire, separators=(",", ":"))),
                "description": len(wire.get("description", "")),
                "input_schema": len(json.dumps(wire.get("inputSchema", {}), separators=(",", ":"))),
                "output_schema": len(
                    json.dumps(wire.get("outputSchema", {}), separators=(",", ":"))
                ),
            }
        )
    rows.sort(key=lambda r: -r["total"])
    return {
        "tool_count": len(rows),
        "total_bytes": sum(r["total"] for r in rows),
        "description_bytes": sum(r["description"] for r in rows),
        "input_schema_bytes": sum(r["input_schema"] for r in rows),
        "output_schema_bytes": sum(r["output_schema"] for r in rows),
        "tools": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--top", type=int, default=8)
    args = parser.parse_args()

    if "INKSCAPE_MCP_WORKSPACE_ROOTS" not in os.environ:
        os.environ["INKSCAPE_MCP_WORKSPACE_ROOTS"] = tempfile.mkdtemp(
            prefix="inkscape-mcp-measure-"
        )

    report = measure()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"tools:           {report['tool_count']}")
    print(f"total bytes:     {report['total_bytes']:,}")
    print(f"  descriptions:  {report['description_bytes']:,}")
    print(f"  input schemas: {report['input_schema_bytes']:,}")
    print(f"  output schemas:{report['output_schema_bytes']:,}")
    print(f"largest {args.top}:")
    for row in report["tools"][: args.top]:
        print(
            f"  {row['name']:<28}{row['total']:>8,}  "
            f"(desc {row['description']:,}, in {row['input_schema']:,}, "
            f"out {row['output_schema']:,})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
