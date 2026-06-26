"""Registered-prompt index resource.

The shipped MCP prompts (`live_canvas_assist`, the export/recolor library, the
authoring library) ARE registered via `@mcp.prompt` and are reachable through the MCP prompts API
(`prompts/list` + `prompts/get`). But an agent driving the server through the RESOURCE surface
(`ListMcpResourcesTool` / `ReadMcpResourceTool`) could not see them at all — prompts are a separate
MCP capability from resources — so it would have to read the server source to learn they exist.

This static (no-placeholder) `inkscape://prompts` resource closes that gap: it indexes every
registered prompt (name + one-line purpose + its arguments) from the LIVE FastMCP registry
(`mcp.list_prompts()` — the same async accessor `list_capabilities`/`gen_llms_txt.py` use for
tools), so the prompt library is DISCOVERABLE through `ListMcpResourcesTool` without reading source.
The index names each prompt so the agent can then fetch its full text over the MCP prompts API
(`prompts/get`). Host-path-free: only prompt names, one-line purposes, and argument descriptors are
emitted (read-only). Risk class: low.
"""

from __future__ import annotations

import asyncio
import json

from inkscape_mcp.server import mcp


def _one_line_purpose(description: str | None) -> str:
    """First non-empty docstring line — the prompt's one-line purpose (mirrors gen_llms_txt)."""
    for line in (description or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return "(no description)"


def _prompt_index() -> list[dict[str, object]]:
    """Read the LIVE FastMCP prompt surface: name + one-line purpose + arguments, sorted by name.

    Uses `mcp.list_prompts()` so the index can never drift from the registered `@mcp.prompt`s. Each
    prompt's `arguments` are surfaced as `{name, description, required}` so a caller knows how to
    invoke it over `prompts/get` without reading source.
    """
    prompts = asyncio.run(mcp.list_prompts())
    index: list[dict[str, object]] = []
    for p in sorted(prompts, key=lambda pr: pr.name):
        arguments = [
            {
                "name": arg.name,
                "description": arg.description or "",
                "required": bool(getattr(arg, "required", False)),
            }
            for arg in (p.arguments or [])
        ]
        index.append(
            {
                "name": p.name,
                "purpose": _one_line_purpose(p.description),
                "arguments": arguments,
            }
        )
    return index


@mcp.resource("inkscape://prompts", mime_type="application/json")
def prompts_index() -> str:
    """Index of registered MCP prompts (name + one-line purpose + arguments).

    Makes the shipped prompt library (`live_canvas_assist`, the export/recolor library, the
    authoring library) discoverable through the RESOURCE surface (`ListMcpResourcesTool`), which
    otherwise cannot see prompts at all. Fetch a prompt's full text via the MCP prompts API
    (`prompts/get`) using the name from this index.

    Shape: ``{"prompt_count": N, "prompts": [{name, purpose, arguments: [{name, description,
    required}]}, ...]}``. Sourced from the live registry (`mcp.list_prompts()`), so it cannot drift
    from the registered prompts.

    Risk class: low (read-only resource).
    """
    index = _prompt_index()
    return json.dumps({"prompt_count": len(index), "prompts": index})
