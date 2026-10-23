"""`history(action='undo')`: walks back edit by edit and stays reversible."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from tests.tool_support import call, open_doc, working_svg


def _edit(app, doc_id, ops):
    return call(app, "edit", {"doc_id": doc_id, "ops": ops}).data


def _hist(app, doc_id, action, **kw):
    return call(app, "history", {"doc_id": doc_id, "action": action, **kw}).data


def _fill(app, doc_id, color):
    return _edit(app, doc_id, [{"op": "fill", "ids": ["r1"], "color": color}])


def test_undo_reverts_only_the_last_edit(tool_app, tool_ws) -> None:
    doc_id = open_doc(tool_ws)
    _fill(tool_app, doc_id, "#00ff00")
    after_first = working_svg(doc_id)
    _fill(tool_app, doc_id, "#0000ff")

    res = _hist(tool_app, doc_id, "undo")

    assert working_svg(doc_id) == after_first  # not the state before the FIRST edit
    assert len(res["undone"]) == 1 and res["undone"][0].startswith("before:")
    assert res["restored_state"].startswith("the state just before edit '")
    assert res["remaining_undoable"] == 1
    assert res["pre_restore_snapshot_id"]


def test_second_undo_walks_back_and_undo_is_reversible(tool_app, tool_ws) -> None:
    doc_id = open_doc(tool_ws)
    original = working_svg(doc_id)
    _fill(tool_app, doc_id, "#00ff00")
    after_first = working_svg(doc_id)
    _fill(tool_app, doc_id, "#0000ff")
    after_second = working_svg(doc_id)

    first = _hist(tool_app, doc_id, "undo")
    assert working_svg(doc_id) == after_first
    _hist(tool_app, doc_id, "undo")
    assert working_svg(doc_id) == original  # walked back, did not toggle

    with pytest.raises(ToolError, match="nothing to undo"):
        _hist(tool_app, doc_id, "undo")

    # undo of the first undo via its pre-restore snapshot, then undo again undoes edit 2 again
    _hist(tool_app, doc_id, "restore", snapshot_id=first["pre_restore_snapshot_id"])
    assert working_svg(doc_id) == after_second
    _hist(tool_app, doc_id, "undo")
    assert working_svg(doc_id) == after_first


def test_undo_steps_and_edit_after_undo(tool_app, tool_ws) -> None:
    doc_id = open_doc(tool_ws)
    original = working_svg(doc_id)
    _fill(tool_app, doc_id, "#00ff00")
    _fill(tool_app, doc_id, "#0000ff")
    _hist(tool_app, doc_id, "undo", steps=2)
    assert working_svg(doc_id) == original

    _fill(tool_app, doc_id, "#123456")
    _hist(tool_app, doc_id, "undo")
    assert working_svg(doc_id) == original
    with pytest.raises(ToolError, match="nothing to undo"):
        _hist(tool_app, doc_id, "undo")


def test_undo_with_no_edits_is_actionable(tool_app, tool_ws) -> None:
    doc_id = open_doc(tool_ws)
    with pytest.raises(ToolError, match=r"nothing to undo.*history"):
        _hist(tool_app, doc_id, "undo")


def test_undo_steps_too_many_and_misuse(tool_app, tool_ws) -> None:
    doc_id = open_doc(tool_ws)
    _fill(tool_app, doc_id, "#00ff00")
    with pytest.raises(ToolError, match="only 1"):
        _hist(tool_app, doc_id, "undo", steps=2)
    with pytest.raises(ToolError, match="steps"):
        _hist(tool_app, doc_id, "list", steps=2)


def test_restore_result_has_pasteable_undo_and_names_the_state_before_the_edit(
    tool_app, tool_ws
) -> None:
    doc_id = open_doc(tool_ws)
    original = working_svg(doc_id)
    _fill(tool_app, doc_id, "#00ff00")
    edited = working_svg(doc_id)
    snap = _hist(tool_app, doc_id, "list")["snapshots"][0]["id"]

    res = _hist(tool_app, doc_id, "restore", snapshot_id=snap)

    assert working_svg(doc_id) == original
    assert res["restored_state"].endswith("that edit is undone")
    assert "undo this with the `undo` call below" in res["note"]
    assert res["undo"] == (
        f"history(doc_id='{doc_id}', action='restore', "
        f"snapshot_id='{res['pre_restore_snapshot_id']}')"
    )
    _hist(tool_app, doc_id, "restore", snapshot_id=res["pre_restore_snapshot_id"])  # run the undo
    assert working_svg(doc_id) == edited
