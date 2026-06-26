"""`readOnlyHint` decoupling drift-guard.

`readOnlyHint` is derived from the explicit `READ_ONLY_TOOLS` set, NOT from `risk == "low"`. This
guard asserts:

- the wire `readOnlyHint` equals `READ_ONLY_TOOLS` membership for every registered tool (the new
  mechanism is the one in force),
- behaviour is PRESERVED: `readOnlyHint` still equals `(risk == "low")` for the current surface —
  this is a refactor with no wire-visible change,
- `annotations_for` derives `readOnlyHint` independent of the `risk` argument (passing a different
  risk does not change it), so the two axes are genuinely decoupled,
- no stale name in `READ_ONLY_TOOLS`.

Both flags forced ON so `list_tools()` reflects the full annotated surface.
"""

from __future__ import annotations

import asyncio
import os

from inkscape_mcp.config import ENV_LIVE_ENABLED, ENV_RAW_ACTION_ENABLED, get_settings
from inkscape_mcp.server import mcp, register_tools
from inkscape_mcp.tool_annotations import READ_ONLY_TOOLS, annotations_for
from inkscape_mcp.tools.system import _risk_class

os.environ[ENV_LIVE_ENABLED] = "1"
os.environ[ENV_RAW_ACTION_ENABLED] = "1"
get_settings.cache_clear()
register_tools()


def _tools() -> list:
    return asyncio.run(mcp.list_tools())


def test_read_only_hint_derives_from_explicit_set() -> None:
    """Wire `readOnlyHint` equals `READ_ONLY_TOOLS` membership — the explicit source of truth."""
    for tool in _tools():
        assert tool.annotations.readOnlyHint is (tool.name in READ_ONLY_TOOLS), (
            f"{tool.name}: readOnlyHint {tool.annotations.readOnlyHint} "
            f"!= (in READ_ONLY_TOOLS={tool.name in READ_ONLY_TOOLS})"
        )


def test_read_only_hint_is_behaviour_preserving() -> None:
    """For the CURRENT surface, the new derivation matches the old `risk == 'low'` mapping."""
    for tool in _tools():
        risk = _risk_class(tool.description)
        assert tool.annotations.readOnlyHint is (risk == "low"), (
            f"{tool.name}: readOnlyHint changed vs (risk=='low'); "
            "decoupling must preserve behaviour for the present surface"
        )


def test_annotations_for_ignores_risk_argument() -> None:
    """`readOnlyHint` is independent of the `risk` arg — the two axes are decoupled."""
    read_only_name = next(iter(READ_ONLY_TOOLS))
    # Same name, every risk tier → identical readOnlyHint (driven by the set, not the risk arg).
    hints = {annotations_for(read_only_name, r).readOnlyHint for r in ("low", "medium", "high")}
    assert hints == {True}, "readOnlyHint must not vary with the risk argument"


def test_no_stale_names_in_read_only_set() -> None:
    """Every `READ_ONLY_TOOLS` entry is a registered tool (no stale name after a rename)."""
    names = {t.name for t in _tools()}
    stale = set(READ_ONLY_TOOLS) - names
    assert not stale, f"READ_ONLY_TOOLS references unregistered tool(s): {sorted(stale)}"
