"""Intent → tool discoverability tool (E14-08): ``how_do_i``.

Maps a natural-language goal to the concrete MCP tool name(s) that achieve it, and flags clearly
out-of-scope goals (raster/pixel editing, arbitrary Actions/extensions/scripts, network fetch, code
execution) with a reason instead of a tool. This is a READ-ONLY guidance tool: it RETURNS tool names
+ a one-line how-to and executes nothing. It is NOT a portmanteau / `do_task` / raw-action hatch
(ADR-002/003) — the actual work still flows through the small typed, gated tools it points at.

The curated goal→tool map, the keyword matcher, and the out-of-scope detector live in the pure
:mod:`inkscape_mcp.intents` module (no MCP, no LLM, no fuzzy lib) so the same map also backs the
`intents` section of `list_capabilities` / `diagnose_runtime` without an import cycle.

Companion tools: `list_capabilities` (browse the whole intents map + runtime matrix),
`inspect_document` (structure + addressable ids), `find_objects` (resolve the object ids the
id-taking edit tools need).

Risk class: low (read-only; no Operation Record / snapshot per ADR-004).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from inkscape_mcp.intents import IntentMatch, detect_out_of_scope, match_intents
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.server import mcp

_logger = get_logger("tools.discover")

#: Suggestion returned when a goal neither matches the curated map nor trips an out-of-scope rule.
_NO_MATCH_NOTE = (
    "No confident match. Browse the full goal→tool map via list_capabilities (its `intents` "
    "section), or call inspect_document to see a document's structure and addressable ids."
)


class HowDoIResult(BaseModel):
    """Result of `how_do_i`: matched guidance, or an explicit out-of-scope / no-match note.

    Exactly one of three shapes:
    * in-scope hit — `out_of_scope=False`, `matches` non-empty (best first), `note` empty;
    * out-of-scope — `out_of_scope=True`, `matches` empty, `note` names WHY it is unsupported;
    * no match — `out_of_scope=False`, `matches` empty, `note` suggests list_capabilities /
      inspect_document.

    Nothing here executes; each `IntentMatch.tools` is a real registered tool name the caller
    invokes itself.
    """

    goal: str = Field(description="The goal string that was matched (echoed back).")
    matches: list[IntentMatch] = Field(
        default_factory=list,
        description="Best-matching guidance entries (tool name(s) + one-line how-to + group).",
    )
    out_of_scope: bool = Field(
        default=False,
        description="True when the goal is a known out-of-scope category (see `note` for why).",
    )
    note: str = Field(
        default="",
        description="Reason (out-of-scope) or suggestion (no match); empty on a confident match.",
    )


@mcp.tool
def how_do_i(goal: str) -> HowDoIResult:
    """Map a natural-language goal to the concrete inkscape-mcp tool(s) that achieve it.

    When to use: when you know what you want in words but not which typed tool does it. To browse
    the whole map at once read the `intents` section of `list_capabilities`; to then resolve an
    object id for an id-taking edit use `find_objects`. Guidance only — not a portmanteau or raw
    tool (ADR-002/003); it executes nothing.

    Key params: `goal` is a natural-language description, e.g. "draw a rectangle", "make my svg
    smaller for web", "find the red shapes", "export a png".

    Return shape: `HowDoIResult` — exactly one of: an in-scope hit (`out_of_scope=False`, `matches`
    best-first, each `{goal_pattern, tools, how_to, group}`); an out-of-scope goal (edit a
    JPEG/photo's pixels, run an arbitrary Action/extension/script, fetch from a URL, execute code) →
    `out_of_scope=True`, empty `matches`, `note` naming WHY (vector-only / ADR-003 / no-network /
    no-exec); or no match → `out_of_scope=False`, empty `matches`, `note` suggesting
    `list_capabilities` / `inspect_document`.

    Example: `how_do_i("make my svg smaller for web")`

    Risk class: low (read-only guidance; no snapshot / Operation Record).
    """
    rule = detect_out_of_scope(goal)
    if rule is not None:
        log_tool_call(_logger, tool="how_do_i", out_of_scope=True, label=rule.label)
        return HowDoIResult(goal=goal, matches=[], out_of_scope=True, note=rule.reason)

    matches = match_intents(goal)
    if not matches:
        log_tool_call(_logger, tool="how_do_i", out_of_scope=False, matched=0)
        return HowDoIResult(goal=goal, matches=[], out_of_scope=False, note=_NO_MATCH_NOTE)

    log_tool_call(_logger, tool="how_do_i", out_of_scope=False, matched=len(matches))
    return HowDoIResult(goal=goal, matches=matches, out_of_scope=False, note="")
