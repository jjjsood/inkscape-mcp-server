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
import re
from threading import Lock

from inkscape_mcp.runtime.probe import Capabilities, ToolInfo, probe_capabilities

# Module-level cache shared with the runtime resource. Guarded by a lock so a concurrent
# re-probe never hands out a half-built matrix.
_cache_lock = Lock()
_cached: Capabilities | None = None

#: The canonical risk vocabulary; matches the derivation in `scripts/gen_llms_txt.py` so the
#: capability map and `llms.txt` report the same risk token for a tool.
_RISK_RE = re.compile(r"Risk class:\s*(?P<risk>low|medium|high|restricted)", re.IGNORECASE)


def _one_line_purpose(description: str | None) -> str:
    """First non-empty docstring line — the tool's one-line purpose (mirrors gen_llms_txt)."""
    for line in (description or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return "(no description)"


def _risk_class(description: str | None) -> str:
    """Parse the `Risk class:` token from a docstring, or `unknown` (mirrors gen_llms_txt)."""
    match = _RISK_RE.search(description or "")
    return match.group("risk").lower() if match else "unknown"


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
                purpose=_one_line_purpose(t.description),
                risk=_risk_class(t.description),
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

    Shared by `list_capabilities` and the `inkscape://runtime/capabilities` resource so they
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
