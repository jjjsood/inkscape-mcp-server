"""Surface shape of the result-oriented tools: tool count, no output schema, small payload."""

from __future__ import annotations

import asyncio
import json

from inkscape_mcp.edit.op_docs import OP_EXAMPLES, OP_GROUPS, op_models
from inkscape_mcp.edit.ops import parse_ops
from inkscape_mcp.tools.v2 import build_app

EXPECTED = {
    "open",
    "inspect",
    "edit",
    "render",
    "export",
    "history",
    "help",
    "live",
    "live_edit",
}


def _tools(advanced: bool) -> dict[str, object]:
    app = build_app(advanced=advanced)
    return {t.name: t for t in asyncio.run(app.list_tools())}


def test_default_surface_has_nine_tools_and_advanced_adds_actions() -> None:
    assert set(_tools(False)) == EXPECTED
    assert set(_tools(True)) == EXPECTED | {"actions"}


def test_no_tool_declares_an_output_schema() -> None:
    for tool in _tools(True).values():
        assert tool.to_mcp_tool().outputSchema is None  # type: ignore[attr-defined]


def test_descriptions_stay_short() -> None:
    for name, tool in _tools(True).items():
        assert len(tool.description or "") <= 650, name  # type: ignore[attr-defined]


def test_tools_list_payload_is_small() -> None:
    total = 0
    for tool in _tools(True).values():
        mcp_tool = tool.to_mcp_tool()  # type: ignore[attr-defined]
        total += len(json.dumps(mcp_tool.model_dump(exclude_none=True), separators=(",", ":")))
    assert total <= 45_000, total
    edit = _tools(True)["edit"].to_mcp_tool()  # type: ignore[attr-defined]
    assert len(json.dumps(edit.model_dump(exclude_none=True), separators=(",", ":"))) <= 15_000


def test_every_op_has_a_group_and_a_valid_example() -> None:
    names = set(op_models())
    assert names == set(OP_EXAMPLES)
    assert names == {n for group in OP_GROUPS.values() for n in group}
    for name, example in OP_EXAMPLES.items():
        (typed,) = parse_ops([example])
        assert typed.op == name
