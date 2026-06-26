"""Runtime capability resource (E1-03) and curated intents resource (E14-08b).

`runtime_capabilities` serves the same cached capability matrix as the `list_capabilities` tool via
the shared cache in `inkscape_mcp.tools.system`, so the resource and the tool always agree for a
given probe; `diagnose_runtime` is what forces a re-probe.

`runtime_intents` serves ONLY the curated goal → tool(s) map (no probe payload) via the shared
`inkscape_mcp.intents.intents_summary_json` accessor — the same in-memory map `how_do_i` and the
capabilities `intents` section use — so the three discovery surfaces cannot drift.
"""

from __future__ import annotations

from inkscape_mcp.intents import intents_summary_json
from inkscape_mcp.server import mcp
from inkscape_mcp.tools.system import get_cached_capabilities


@mcp.resource("inkscape://runtime/capabilities", mime_type="application/json")
def runtime_capabilities() -> str:
    """Probed Inkscape/runtime capability matrix (version, actions, export formats, fonts, live).

    Returns the same cached matrix as the `list_capabilities` tool, including the authoritative
    MCP tool surface (`tool_count` + `tools`, from the live registry — E16-01). Use the
    `diagnose_runtime` tool to force a fresh probe.

    Risk class: low (read-only).
    """
    return get_cached_capabilities().model_dump_json()


@mcp.resource("inkscape://runtime/intents", mime_type="application/json")
def runtime_intents() -> str:
    """Curated natural-language goal → tool(s) intent map, WITHOUT the full capabilities payload.

    Shape: ``{"intents": [{goal_pattern, tools, how_to, group}, ...]}``. This is the exact same
    curated map the `how_do_i` tool matches against and the `list_capabilities` `intents` section
    exposes — all three derive from `inkscape_mcp.intents.intents_summary`, so they cannot drift.
    Guidance only: it names which tool(s) achieve a goal, executes nothing, and offers no raw-action
    escape hatch (ADR-003). Host-independent (not probed), so it needs no Inkscape binary.

    Use this when you want just the discovery map without the heavier runtime probe matrix.

    Risk class: low (read-only).
    """
    return intents_summary_json()
