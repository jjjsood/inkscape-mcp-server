"""Surface shape of the registered server: tool set, annotations, payload budget, gating."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from inkscape_mcp.edit.op_docs import OP_EXAMPLES, OP_GROUPS, op_models
from inkscape_mcp.edit.ops import parse_ops
from inkscape_mcp.server import mcp, register_tools
from inkscape_mcp.tool_annotations import _ANNOTATIONS
from inkscape_mcp.tool_tags import TOOL_TAGS

CORE = {"open", "inspect", "edit", "render", "export", "history", "help"}
LIVE = {"live", "live_edit"}
ADVANCED = {"actions"}


def _tools() -> dict[str, Any]:
    register_tools()
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def _wire_size(tool: Any) -> int:
    wire = tool.to_mcp_tool().model_dump(exclude_none=True, by_alias=True)
    return len(json.dumps(wire, separators=(",", ":")))


def test_default_test_surface_has_all_ten_tools() -> None:
    assert set(_tools()) == CORE | LIVE | ADVANCED


def test_no_tool_declares_an_output_schema() -> None:
    for name, tool in _tools().items():
        assert tool.to_mcp_tool().outputSchema is None, name


def test_descriptions_stay_short() -> None:
    for name, tool in _tools().items():
        assert len(tool.description or "") <= 650, name


def test_tools_list_payload_is_small() -> None:
    tools = _tools()
    assert sum(_wire_size(t) for t in tools.values()) <= 45_000
    assert _wire_size(tools["edit"]) <= 15_000


def test_every_tool_has_annotations_and_a_tag() -> None:
    tools = _tools()
    assert set(_ANNOTATIONS) == set(TOOL_TAGS) == set(tools)
    for name, tool in tools.items():
        assert tool.annotations is not None, name
        assert tool.annotations.title, name
    read_only = {n for n, t in tools.items() if t.annotations.readOnlyHint}
    assert read_only == {"inspect", "render", "help"}
    destructive = {n for n, t in tools.items() if t.annotations.destructiveHint}
    assert destructive == {"edit", "history", "export", "live_edit", "actions"}
    open_world = {n for n, t in tools.items() if t.annotations.openWorldHint}
    assert open_world == LIVE | ADVANCED


def test_schemas_are_compact() -> None:
    for name, tool in _tools().items():
        text = json.dumps(tool.parameters)
        assert '"title"' not in text, name
        assert '"default": null' not in text, name


def test_flags_gate_the_live_and_advanced_groups(surface_with_flags: Any) -> None:
    assert surface_with_flags(live=True, raw=True) == CORE | LIVE | ADVANCED
    assert surface_with_flags(live=False, raw=True) == CORE | ADVANCED
    assert surface_with_flags(live=True, raw=False) == CORE | LIVE
    assert surface_with_flags(live=False, raw=False) == CORE
    assert surface_with_flags(live=True, raw=True) == CORE | LIVE | ADVANCED


def test_every_op_has_a_group_and_a_valid_example() -> None:
    names = set(op_models())
    assert names == set(OP_EXAMPLES)
    assert names == {n for group in OP_GROUPS.values() for n in group}
    for name, example in OP_EXAMPLES.items():
        (typed,) = parse_ops([example])
        assert typed.op == name
