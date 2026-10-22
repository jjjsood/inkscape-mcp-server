"""`history`: list / create / restore / prune / reload."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from tests.v2_support import call, open_doc, working_svg


def _hist(app, doc, action, **kw):
    return call(app, "history", {"doc_id": doc, "action": action, **kw}).data


def _recolor(app, doc, color):
    call(app, "edit", {"doc_id": doc, "ops": [{"op": "fill", "ids": ["r1"], "color": color}]})


def test_list_create_and_restore_roundtrip(v2_app, v2_ws) -> None:
    doc = open_doc(v2_ws)
    original = working_svg(doc)
    assert "snapshots" not in _hist(v2_app, doc, "list")
    _recolor(v2_app, doc, "#0000ff")
    edited = working_svg(doc)
    assert edited != original
    snaps = _hist(v2_app, doc, "list")["snapshots"]
    assert len(snaps) == 1
    restored = _hist(v2_app, doc, "restore", snapshot_id=snaps[0]["id"])
    assert restored["restored_from"] == snaps[0]["id"]
    assert restored["pre_restore_snapshot_id"]
    assert working_svg(doc) == original


def test_create_with_label(v2_app, v2_ws) -> None:
    doc = open_doc(v2_ws)
    made = _hist(v2_app, doc, "create", label="before-logo")
    assert made["snapshot_id"]
    assert _hist(v2_app, doc, "list")["snapshots"][0]["label"] == "before-logo"
    with pytest.raises(ToolError, match="label too long"):
        _hist(v2_app, doc, "create", label="x" * 300)


def test_restore_errors_are_actionable(v2_app, v2_ws) -> None:
    doc = open_doc(v2_ws)
    with pytest.raises(ToolError, match="requires snapshot_id"):
        _hist(v2_app, doc, "restore")
    with pytest.raises(ToolError, match=r"snapshot not found; call history\(action='list'\)"):
        _hist(v2_app, doc, "restore", snapshot_id="snap-missing")


def test_arguments_must_match_the_action(v2_app, v2_ws) -> None:
    doc = open_doc(v2_ws)
    with pytest.raises(ToolError, match="only used with action='restore'"):
        _hist(v2_app, doc, "list", snapshot_id="x")
    with pytest.raises(ToolError, match="only used with action='create'"):
        _hist(v2_app, doc, "list", label="x")
    with pytest.raises(ToolError, match="document id not found"):
        call(v2_app, "history", {"doc_id": "nope"})


def test_reload_restores_the_source_and_is_itself_undoable(v2_app, v2_ws) -> None:
    doc = open_doc(v2_ws)
    original = working_svg(doc)
    _recolor(v2_app, doc, "#0000ff")
    res = _hist(v2_app, doc, "reload")
    assert res["pre_reload_snapshot_id"] and res["summary"]["objects"] >= 3
    assert working_svg(doc) == original
    _hist(v2_app, doc, "restore", snapshot_id=res["pre_reload_snapshot_id"])
    assert "#0000ff" in working_svg(doc)


def test_prune(v2_app, v2_ws) -> None:
    doc = open_doc(v2_ws)
    _recolor(v2_app, doc, "#0000ff")
    res = _hist(v2_app, doc, "prune")
    assert res["doc_id"] == doc and "retained" in res
