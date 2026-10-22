"""`help`: orientation, goals, op reference, topics, stat."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.edit.op_docs import OP_EXAMPLES
from inkscape_mcp.edit.ops import parse_ops
from tests.v2_support import call, open_doc


def _help(app, **args):
    return call(app, "help", args).data


def test_no_arguments_returns_the_workflow(v2_app) -> None:
    data = _help(v2_app)
    assert "open -> edit" in data["flow"]
    assert {"open", "edit", "export"} <= set(data["tools"])


def test_goal_matches_or_says_out_of_scope(v2_app) -> None:
    data = _help(v2_app, goal="export a png")
    assert data["goal"]["matches"]
    out = _help(v2_app, goal="edit the pixels of a jpeg photo")
    assert out["goal"]["out_of_scope"] is True


@pytest.mark.parametrize("name", sorted(OP_EXAMPLES))
def test_every_op_is_documented_with_a_valid_example(v2_app, name) -> None:
    ref = _help(v2_app, op=name)["op"]
    assert ref["op"] == name and ref["does"]
    parse_ops([ref["example"]])


def test_op_reference_shape(v2_app) -> None:
    ref = _help(v2_app, op="delete")["op"]
    assert ref["risk"] == "high" and ref["needs_approval"] is True
    rect = _help(v2_app, op="create_rect")["op"]
    assert rect["creates_id"] is True
    assert rect["fields"]["width"] == "float" and rect["fields"]["rx"].endswith("?")


def test_unknown_op_suggests_similar_names(v2_app) -> None:
    with pytest.raises(ToolError, match=r"unknown op 'creat_rect' \(similar: create_rect"):
        _help(v2_app, op="creat_rect")


def test_topic_ops_lists_every_group(v2_app) -> None:
    groups = _help(v2_app, topic="ops")["ops"]["groups"]
    assert {"create", "style", "object", "transform", "compose", "path", "optimize"} == set(groups)
    assert "create_rect" in groups["create"]


def test_topic_capabilities_omits_the_old_tool_list(v2_app) -> None:
    caps = _help(v2_app, topic="capabilities")["capabilities"]
    assert "tools" not in caps and "intents" not in caps


def test_stat_paths(v2_app, v2_ws) -> None:
    (v2_ws / "a.svg").write_bytes(b"<svg/>")
    stat = _help(v2_app, stat_paths=["a.svg"])["stat"]
    assert stat["artifacts"][0]["bytes"] == 6 and stat["total_bytes"] == 6
    with pytest.raises(ToolError, match="path rejected"):
        _help(v2_app, stat_paths=["../x"])
    with pytest.raises(ToolError, match="at least one path"):
        _help(v2_app, stat_paths=[])


def test_parts_combine(v2_app, v2_ws) -> None:
    open_doc(v2_ws)
    data = _help(v2_app, op="fill", topic="ops")
    assert set(data) == {"op", "ops"}
