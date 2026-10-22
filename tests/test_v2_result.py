"""`tools/v2/_result.py`: compact JSON, the single error map, the guard decorator."""

from __future__ import annotations

import json

import pytest
from fastmcp.exceptions import ToolError
from mcp.types import TextContent

from inkscape_mcp.actions.chains import ActionChainError, ActionEngineError
from inkscape_mcp.document.inspect import DocumentNotFound, InspectionError
from inkscape_mcp.edit.dom import EditError, TargetNotFound
from inkscape_mcp.edit.ops import OpError
from inkscape_mcp.live.transport import LiveDisabled, LiveNotAvailable
from inkscape_mcp.render.cli import InvalidObjectId, RenderError
from inkscape_mcp.snapshots import SnapshotNotFound
from inkscape_mcp.tools._result import compact, dumps, guarded, ok, to_tool_error
from inkscape_mcp.workspace.limits import LimitExceeded
from inkscape_mcp.workspace.paths import SandboxViolation
from inkscape_mcp.workspace.risk import PolicyViolation
from inkscape_mcp.workspace.subprocess_exec import ProcessError


def test_compact_drops_none_and_empties_but_keeps_false_and_zero() -> None:
    value = {"a": None, "b": [], "c": {}, "i": "", "d": False, "e": 0, "f": [{"g": None, "h": 1}]}
    assert compact(value) == {"d": False, "e": 0, "f": [{"h": 1}]}


def test_dumps_is_minified() -> None:
    assert dumps({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'


def test_ok_has_one_text_block_and_no_structured_content() -> None:
    result = ok({"x": 1})
    assert result.structured_content is None
    (block,) = result.content
    assert isinstance(block, TextContent)
    assert json.loads(block.text) == {"x": 1}


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (KeyError("x"), "document id not found"),
        (DocumentNotFound("x"), "document id not found"),
        (TargetNotFound("x"), "object id not found in document"),
        (InspectionError("x"), "document could not be parsed safely"),
        (OpError("ops[2] fill: id 'a' not found"), "ops[2] fill: id 'a' not found"),
        (EditError("bad colour"), "bad colour"),
        (PolicyViolation("needs approval"), "needs approval"),
        (SandboxViolation("path rejected: outside workspace"), "path rejected: outside workspace"),
        (InvalidObjectId("x"), "object id is not valid"),
        (RenderError("boom"), "render/export failed"),
        (SnapshotNotFound("x"), "snapshot not found; call history(action='list') for valid ids"),
        (ValueError("secret /host/path"), "operation failed"),
    ],
)
def test_error_map(exc: Exception, expected: str) -> None:
    assert str(to_tool_error(exc)) == expected


def test_limit_message_is_per_call() -> None:
    assert str(to_tool_error(LimitExceeded("x"), limit_msg="too big")) == "too big"


def test_engine_absence_points_at_help_diagnose() -> None:
    assert "help(topic='diagnose')" in str(to_tool_error(ProcessError("no binary")))
    absent = ActionEngineError("inkscape engine unavailable")
    assert "help(topic='diagnose')" in str(to_tool_error(absent))


def test_chain_error_for_absent_action_names_the_list_action() -> None:
    err = to_tool_error(ActionChainError("action_absent", "not here"))
    assert str(err).startswith("action_absent: not here")
    assert "actions(action='list')" in str(err)


def test_live_errors_name_the_new_tools() -> None:
    assert "live(action='check')" in str(to_tool_error(LiveDisabled("x")))
    assert "live(action='connect')" in str(to_tool_error(LiveNotAvailable("x")))


def test_tool_error_passes_through_and_guard_maps_everything_else() -> None:
    original = ToolError("already fine")
    assert to_tool_error(original) is original

    @guarded()
    def boom() -> None:
        raise KeyError("x")

    with pytest.raises(ToolError, match="document id not found"):
        boom()
