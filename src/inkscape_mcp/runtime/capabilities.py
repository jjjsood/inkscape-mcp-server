"""Runtime capability matrix: probe-once cache plus the live MCP tool-surface overlay.

`get_cached_capabilities` returns a cached probe (probe-once, reuse); `refresh_capabilities`
forces a fresh probe and refreshes that cache. The `inkscape://runtime/capabilities` resource and
the capability tools share this cache, so tool and resource always agree for a given probe.

The matrix also carries the authoritative MCP tool surface — `tool_count` + `tools` (name + one-line
purpose + risk class) — sourced from the LIVE FastMCP registry (`mcp.list_tools()`), the same single
source of truth `scripts/gen_llms_txt.py` reads. The pure probe (`inkscape_mcp.runtime.probe`) stays
MCP-free; this layer overlays the registry fields with `with_registry_tools` before the matrix is
served, so the one count can never drift from the registered surface.
"""

from __future__ import annotations

import asyncio
from threading import Lock
from typing import Any

from inkscape_mcp.runtime.probe import Capabilities, ToolInfo, probe_capabilities

# Module-level cache shared with the runtime resource. Guarded by a lock so a concurrent
# re-probe never hands out a half-built matrix.
_cache_lock = Lock()
_cached: Capabilities | None = None

#: Worst-case risk class per tool. `risk_for(tool, None)` reports this ceiling; with the call's
#: arguments `risk_for` narrows it (an `edit` of only style ops is MEDIUM, not HIGH).
_TOOL_RISK: dict[str, str] = {
    "open": "medium",
    "inspect": "low",
    "edit": "high",
    "render": "low",
    "export": "high",
    "history": "medium",
    "help": "low",
    "live": "medium",
    "live_edit": "high",
    "actions": "high",
}


def one_line_purpose(description: str | None) -> str:
    """First non-empty description line: the tool/prompt's one-line purpose."""
    for line in (description or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return "(no description)"


def risk_for(tool: str, args: dict[str, Any] | None = None) -> str:
    """Risk class (`low`/`medium`/`high`) of one call to `tool`, or its ceiling when `args` is None.

    `edit` and `open` take the max over the ops in `args["ops"]` (the same `ops_risk` the kernel
    enforces); `export` is HIGH only when a `save_as` target overwrites. An unparseable `ops` list
    falls back to the ceiling, so the answer never understates.
    """
    ceiling = _TOOL_RISK.get(tool, "unknown")
    if args is None:
        return ceiling
    if tool in ("edit", "open"):
        from inkscape_mcp.edit.ops import OpError, ops_risk, parse_ops

        raw = args.get("ops")
        if not raw:
            return "medium"
        try:
            return str(ops_risk(parse_ops(raw)).value)
        except OpError:
            return ceiling
    if tool == "export":
        targets = args.get("targets") or []
        overwrites = any(
            isinstance(t, dict) and t.get("kind") == "save_as" and t.get("overwrite")
            for t in targets
        )
        return "high" if overwrites else "medium"
    return ceiling


def _registry_tools() -> list[ToolInfo]:
    """Read the LIVE FastMCP tool surface: name + one-line purpose + risk class.

    Uses `mcp.list_tools()` — the same async registry accessor `scripts/gen_llms_txt.py` and the
    drift-guard tests read — so the authoritative tool count is one number that cannot drift from
    the registered `@mcp.tool`s. Sorted by name for a stable, deterministic order. Counts only
    tools (not resources or prompts).
    """
    # Imported lazily: the pure probe layer stays MCP-free and importing the server here at module
    # load would create an import cycle (server -> tools -> runtime.capabilities).
    from inkscape_mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    return sorted(
        (
            ToolInfo(
                name=t.name,
                purpose=one_line_purpose(t.description),
                risk=risk_for(t.name),
            )
            for t in tools
        ),
        key=lambda t: t.name,
    )


def with_registry_tools(caps: Capabilities) -> Capabilities:
    """Return a copy of `caps` with `tool_count` + `tools` populated from the live registry.

    The pure probe leaves these empty (it has no MCP dependency); this overlay is applied before
    the matrix is served so tool and resource both carry the authoritative surface.
    """
    tool_infos = _registry_tools()
    return caps.model_copy(update={"tools": tool_infos, "tool_count": len(tool_infos)})


def get_cached_capabilities() -> Capabilities:
    """Return the cached capability matrix, probing once on first use.

    Shared by `help(topic=...)` and the `inkscape://runtime/capabilities` resource so they
    return identical data for the same probe. The live MCP tool surface is overlaid on
    every read, so a tool registered after the probe is still reflected.
    """
    global _cached
    with _cache_lock:
        if _cached is None:
            _cached = probe_capabilities()
        cached = _cached
    return with_registry_tools(cached)


def refresh_capabilities() -> Capabilities:
    """Run a fresh probe, store it as the cache, and return it (with the live tool surface)."""
    global _cached
    fresh = probe_capabilities()
    with _cache_lock:
        _cached = fresh
    return with_registry_tools(fresh)
