"""Short-form tool-description mode (E20-03).

The E15-02 6-part docstring template (summary · when-to-use · key params · render-and-look ·
return shape · example · risk class) gives excellent discoverability, but the full `tools/list`
JSON is heavy in EVERY turn's context. E18-03 trims the tool COUNT (`tool_profile=core`); this
trims the description LENGTH instead, orthogonally: when `INKSCAPE_MCP_TOOL_DESC=short`
(:data:`inkscape_mcp.config.TOOL_DESC_SHORT`) the wire description for each tool is replaced by a
DERIVED short form — the first "what it does" line plus the `Risk class:` line — dropping the
when-to-use / render-and-look / return-shape / example prose.

Two invariants make this safe:

- The short form is DERIVED from the canonical docstring by :func:`short_description` — there is no
  second, hand-maintained description anywhere, so it can never drift from the real docstring
  (the drift-guard test asserts exactly this).
- The JSON `inputSchema` (param names + types) is untouched, so a caller still sees every argument;
  only the human-prose duplication of the parameters is dropped from the description.

`gen_llms_txt` forces `full`, so the committed `llms.txt` / `llms-full.txt` always carry the
COMPLETE catalog regardless of this knob.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

#: Start of the `Risk class:` paragraph the E15-02 template guarantees on every tool — the SAME
#: marker the capability map / `gen_llms_txt` rely on. The paragraph itself can WRAP over several
#: lines; `_risk_paragraph` collects the whole of it (to the next blank line / end) so the risk tier
#: and the approval-gate hint for high-risk tools survive the trim intact, never cut mid-sentence.
_RISK_START = re.compile(r"^[ \t]*Risk class\b", re.IGNORECASE)


def _leading_paragraph(lines: list[str]) -> str:
    """The first paragraph — consecutive non-blank lines up to the first blank line — joined."""
    para: list[str] = []
    for line in lines:
        if not line.strip():
            if para:
                break
            continue
        para.append(line.strip())
    return " ".join(para).strip()


def _risk_paragraph(lines: list[str]) -> str:
    """The full `Risk class:` paragraph (its start line + any wrapped lines), joined, or ``""``."""
    para: list[str] = []
    capturing = False
    for line in lines:
        if not capturing and _RISK_START.match(line):
            capturing = True
        if capturing:
            if not line.strip():
                break
            para.append(line.strip())
    return " ".join(para).strip()


def short_description(full: str) -> str:
    """Derive the short-form description from a full E15-02 docstring.

    Keeps the FIRST paragraph (the "what it does" summary, one or more non-blank lines up to the
    first blank line) and the whole `Risk class:` paragraph; drops everything else. Returns the
    input unchanged when it has no blank-line structure to trim (already short / non-templated).
    Pure function of the input — the single source of truth is the canonical docstring, no copy.
    """
    if not full or not full.strip():
        return full

    lines = full.splitlines()
    summary = _leading_paragraph(lines)
    risk = _risk_paragraph(lines)

    if risk and risk not in summary:
        return f"{summary}\n\n{risk}"
    return summary


#: Canonical FULL description per tool name, captured the first time a tool is shortened (BEFORE the
#: trim, so it always holds the genuine docstring). Makes `apply_short_descriptions` reversible and
#: order-independent: `full` restores from here, `short` re-derives from here. `register_tools()`
#: mutates the SHARED `mcp` Tool objects in place, so without this a once-shortened description
#: could never be restored (and a later `gen_llms_txt` / a test expecting the full surface would
#: see the trimmed text).
_FULL_CACHE: dict[str, str] = {}


def apply_short_descriptions(app: FastMCP) -> None:
    """Set every registered tool's wire description to the short or full form per the knob.

    Called LAST in `register_tools()` — AFTER `apply_annotations` (which parses the risk class from
    the description) — so shortening never starves an earlier derivation. Mutates each stored `Tool`
    object's `description` in place over the sync component registry (the same pattern as
    `apply_annotations`), so the change flows straight to the wire `tools/list` and works whether or
    not an event loop is running.

    Reversible via :data:`_FULL_CACHE`: under `short` the canonical full description is cached once
    (before the trim) and the short form served; under `full` (default) any cached full description
    is restored, so flipping the knob — or re-registering in the same process — always lands on the
    correct text.
    """
    from fastmcp.tools import Tool

    from inkscape_mcp.config import TOOL_DESC_SHORT, get_settings

    short_mode = get_settings().tool_desc == TOOL_DESC_SHORT

    for component in app._local_provider._components.values():
        if not isinstance(component, Tool):
            continue
        if short_mode:
            if component.description:
                full = _FULL_CACHE.setdefault(component.name, component.description)
                component.description = short_description(full)
        elif component.name in _FULL_CACHE:
            component.description = _FULL_CACHE[component.name]
