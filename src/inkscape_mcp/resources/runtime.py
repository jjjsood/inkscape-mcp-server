"""Runtime capability resource and curated intents resource.

`runtime_capabilities` serves the same cached capability matrix as `help(topic='capabilities')` via
the shared cache in `inkscape_mcp.runtime.capabilities`, so the resource and the tool always agree
for a given probe; `help(topic='diagnose')` is what forces a re-probe.

`runtime_intents` serves ONLY the curated goal → (tool, op) map (no probe payload) via the shared
`inkscape_mcp.intents.intents_summary_json` accessor — the same in-memory map `help(goal=...)` and
the capabilities `intents` section use — so the three discovery surfaces cannot drift.
"""

from __future__ import annotations

import json

from inkscape_mcp.edit.op_docs import ops_reference
from inkscape_mcp.intents import intents_summary_json
from inkscape_mcp.runtime.capabilities import get_cached_capabilities
from inkscape_mcp.server import mcp


@mcp.resource("inkscape://runtime/capabilities", mime_type="application/json")
def runtime_capabilities() -> str:
    """Probed Inkscape/runtime capability matrix (version, actions, export formats, fonts, live).

    Returns the same cached matrix as `help(topic='capabilities')`, including the authoritative
    MCP tool surface (`tool_count` + `tools`, from the live registry). Use
    `help(topic='diagnose')` to force a fresh probe.

    Risk class: low (read-only).
    """
    return get_cached_capabilities().model_dump_json()


@mcp.resource("inkscape://runtime/intents", mime_type="application/json")
def runtime_intents() -> str:
    """Curated natural-language goal → (tool, op) intent map, WITHOUT the full capabilities payload.

    Shape: ``{"intents": [{goal_pattern, targets: [{tool, op}], how_to, group, example}, ...]}``.
    This is the exact same curated map `help(goal=...)` matches against and the capabilities
    `intents` section exposes — all derive from `inkscape_mcp.intents.intents_summary`, so they
    cannot drift. Guidance only: it names which call achieves a goal and shows an example, and
    executes nothing. Host-independent (not probed), so it needs no Inkscape binary.

    Use this when you want just the discovery map without the heavier runtime probe matrix.

    Risk class: low (read-only).
    """
    return intents_summary_json()


@mcp.resource("inkscape://ops", mime_type="application/json")
def ops_resource() -> str:
    """Reference for every `edit` / `open` op: group, fields, risk and a valid example.

    The `edit` tool's schema does not carry the op union, so this resource (and `help(op=NAME)`)
    is where an agent reads the exact fields of an op.

    Risk class: low (read-only).
    """
    return json.dumps(ops_reference(), separators=(",", ":"))
