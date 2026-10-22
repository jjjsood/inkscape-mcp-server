"""Errors through the real tools say what to do next, and never leak a host path."""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from tests.tool_support import call, open_doc

FILL = {"op": "fill", "ids": ["r1"], "color": "#fff"}


def _fail(app: Any, tool: str, args: dict[str, Any]) -> str:
    with pytest.raises(ToolError) as excinfo:
        call(app, tool, args)
    return str(excinfo.value)


def test_unknown_id_names_the_op_and_the_id(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    msg = _fail(tool_app, "edit", {"doc_id": doc, "ops": [FILL, {**FILL, "ids": ["r9"]}]})
    assert msg.startswith("ops[1] fill:") and "'r9'" in msg and "not found" in msg


def test_high_risk_op_asks_for_an_approval_token(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    msg = _fail(tool_app, "edit", {"doc_id": doc, "ops": [{"op": "delete", "ids": ["r1"]}]})
    assert msg.startswith("ops[0] delete is HIGH risk; re-call with approval_token (dry_run=true")


def test_unknown_op_lists_the_valid_ones_and_missing_field_is_named(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    msg = _fail(tool_app, "edit", {"doc_id": doc, "ops": [{"op": "nope"}]})
    assert msg.startswith("invalid ops: ops[0]:") and "'create_rect'" in msg
    msg = _fail(tool_app, "edit", {"doc_id": doc, "ops": [{"op": "fill", "ids": ["r1"]}]})
    assert "ops[0].fill.color: Field required" in msg


def test_help_points_at_the_ops_listing(tool_app) -> None:
    assert "help(topic='ops')" in _fail(tool_app, "help", {"op": "bogus"})


@pytest.mark.parametrize(
    ("tool", "args", "hint"),
    [
        ("history", {"action": "restore", "snapshot_id": "nope"}, "history(action='list')"),
        ("history", {"action": "restore"}, "requires snapshot_id; call history(action='list')"),
        ("render", {"target": "object"}, "requires object_id"),
        ("live", {"action": "observe"}, "live(action='connect')"),
    ],
)
def test_error_names_the_next_call(tool_app, tool_ws, tool, args, hint) -> None:
    doc = open_doc(tool_ws)
    if tool in {"history", "render"}:
        args = {**args, "doc_id": doc}
    assert hint in _fail(tool_app, tool, args)


@pytest.mark.parametrize("path", ["../../etc/passwd", "missing.svg"])
def test_path_errors_do_not_leak_the_host_path(tool_app, tool_ws, path) -> None:
    msg = _fail(tool_app, "open", {"path": path})
    assert msg.startswith("path rejected:")
    assert str(tool_ws) not in msg and "/etc" not in msg


def test_unexpected_failures_are_opaque(tool_app, tool_ws, monkeypatch) -> None:
    doc = open_doc(tool_ws)

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("secret /host/path")

    monkeypatch.setattr("inkscape_mcp.snapshots.create_snapshot", boom)
    msg = _fail(tool_app, "history", {"doc_id": doc, "action": "create"})
    assert "secret" not in msg and "/host" not in msg


@pytest.mark.parametrize(
    ("tool", "args", "where"),
    [
        ("export", {"doc_id": "d", "targets": [{"kind": "nope"}]}, "targets"),
        ("inspect", {"doc_id": "d", "include": ["bogus"]}, "include"),
        (
            "export",
            {"doc_id": "d", "targets": [{"kind": "file", "format": "png"}] * 1 + [{}]},
            "targets",
        ),
    ],
)
def test_invalid_arguments_are_one_short_line(tool_app, tool, args, where) -> None:
    msg = _fail(tool_app, tool, args)
    assert msg.startswith(f"invalid {tool} arguments: ") and where in msg
    assert "\n" not in msg and "pydantic.dev" not in msg and "input_value" not in msg
    assert msg.count(";") <= 2  # at most three problems


def test_unknown_doc_id_lists_open_ids(tool_app, tool_ws) -> None:
    assert "no document is open" in _fail(tool_app, "inspect", {"doc_id": "d_gone"})
    doc = open_doc(tool_ws)
    msg = _fail(tool_app, "inspect", {"doc_id": "d_gone"})
    assert msg.startswith("document id not found; open ids: ") and doc in msg
    assert "not a path" in msg


def test_source_path_as_doc_id_names_the_real_doc_id(tool_app, tool_ws) -> None:
    older = open_doc(tool_ws, "a.svg")
    doc = open_doc(tool_ws, "logo.svg")
    # a path is still rejected (doc_id stays strict) but the error names the right id
    for given in ("logo.svg", "./logo.svg", str(tool_ws / "logo.svg")):
        msg = _fail(tool_app, "inspect", {"doc_id": given})
        assert f"'logo.svg' is open as doc_id {doc!r}; pass that doc_id" in msg
        assert str(tool_ws) not in msg
    # unrelated unknown value: list falls back, most recently opened first
    msg = _fail(tool_app, "inspect", {"doc_id": "nope.svg"})
    assert "open ids: " in msg and msg.index(doc) < msg.index(older)
