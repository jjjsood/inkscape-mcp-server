"""`help`: orientation, op reference, the ops topic. Goals/capabilities/stat: help_capabilities."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.edit.op_docs import OP_EXAMPLES
from inkscape_mcp.edit.ops import parse_ops
from tests.tool_support import call


def _help(app, **args):
    return call(app, "help", args).data


def test_no_arguments_returns_the_workflow(tool_app) -> None:
    data = _help(tool_app)
    assert "open -> edit" in data["flow"]
    assert {"open", "edit", "export"} <= set(data["tools"])


@pytest.mark.parametrize("name", sorted(OP_EXAMPLES))
def test_every_op_is_documented_with_a_valid_example(tool_app, name) -> None:
    ref = _help(tool_app, op=name)["op"]
    assert ref["op"] == name and ref["does"]
    parse_ops([ref["example"]])


def test_op_reference_shape(tool_app) -> None:
    ref = _help(tool_app, op="delete")["op"]
    assert ref["risk"] == "high" and ref["needs_approval"] is True
    rect = _help(tool_app, op="create_rect")["op"]
    assert rect["creates_id"] is True
    assert rect["fields"]["width"] == "float" and rect["fields"]["rx"].endswith("?")


def test_unknown_op_suggests_similar_names(tool_app) -> None:
    with pytest.raises(ToolError, match=r"unknown op 'creat_rect' \(similar: create_rect"):
        _help(tool_app, op="creat_rect")


def test_topic_ops_lists_every_group(tool_app) -> None:
    groups = _help(tool_app, topic="ops")["ops"]["groups"]
    assert {"create", "style", "object", "transform", "compose", "path", "optimize"} == set(groups)
    assert "create_rect" in groups["create"]


def test_parts_combine(tool_app) -> None:
    data = _help(tool_app, op="fill", topic="ops")
    assert set(data) == {"op", "ops"}
