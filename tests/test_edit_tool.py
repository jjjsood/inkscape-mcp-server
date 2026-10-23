"""`edit`: detail levels, `$n` references, dry run, risk gate, atomicity, inline render."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from tests.tool_support import call, open_doc, working_svg

RECT = {"op": "create_rect", "x": 0, "y": 0, "width": 50, "height": 20, "id": "bg"}


def _edit(app, doc, ops, **kw):
    return call(app, "edit", {"doc_id": doc, "ops": ops, **kw})


def test_default_result_is_ids_only(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    data = _edit(tool_app, doc, [RECT]).data
    assert set(data) == {"doc_id", "changed", "snapshot_id", "risk", "created", "undo"}
    assert data["undo"] == (
        f"history(doc_id='{doc}', action='restore', snapshot_id='{data['snapshot_id']}')"
    )  # medium results carry the undo call too
    assert data["changed"] is True
    assert data["risk"] == "medium"
    assert data["created"] == [{"op": 0, "id": "bg", "bbox": [0, 0, 50, 20]}]


def test_detail_levels_add_information(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    summary = _edit(tool_app, doc, [RECT], detail="summary").data
    assert summary["ops"][0].startswith("0 create_rect:")
    doc2 = open_doc(tool_ws, "two.svg")
    full = _edit(tool_app, doc2, [RECT], detail="full").data
    assert full["operation_id"] and full["outcomes"][0]["name"] == "create_rect"


def test_dollar_references_and_selectors_see_objects_from_the_same_call(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    data = _edit(
        tool_app,
        doc,
        [
            RECT,
            {"op": "fill", "ids": ["$0"], "color": "#00ff00"},
            {"op": "stroke", "select": {"id_prefix": "bg"}, "color": "#000000"},
        ],
    ).data
    assert data["changed"] is True
    svg = working_svg(doc)
    assert "#00ff00" in svg and "#000000" in svg


def test_failing_op_is_a_noop_and_names_the_op(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    before = working_svg(doc)
    with pytest.raises(ToolError, match=r"ops\[1\] fill: id 'logo2' not found"):
        _edit(tool_app, doc, [RECT, {"op": "fill", "ids": ["logo2"], "color": "#fff"}])
    assert working_svg(doc) == before


def test_invalid_op_shape_reports_field_paths_in_the_kernel_format(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    with pytest.raises(ToolError, match=r"invalid ops: ops\[0\]"):
        _edit(tool_app, doc, [{"op": "create_rect", "x": 1}])
    with pytest.raises(ToolError, match="at least one op"):
        _edit(tool_app, doc, [])


def test_high_risk_needs_approval_and_dry_run_reports_it(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    delete = {"op": "delete", "ids": ["r1"]}
    with pytest.raises(ToolError) as excinfo:
        _edit(tool_app, doc, [delete])
    msg = str(excinfo.value)
    assert msg.startswith("ops[0] delete is HIGH risk; re-call with approval_token (dry_run=true")
    assert "or send the other" not in msg  # a lone high-risk op has no mixed-call hint
    plan = _edit(tool_app, doc, [delete], dry_run=True).data
    assert plan["dry_run"] is True and plan["needs_approval"] is True
    assert plan["changed"] is False and "approved" not in plan
    assert plan["plan"][0]["matched"] == ["r1"]
    assert plan["plan"][0]["summary"].startswith("plan (not applied): ")
    assert 'id="r1"' in working_svg(doc)
    done = _edit(tool_app, doc, [delete], approval_token="yes").data
    assert done["risk"] == "high" and done["changed"] is True
    assert done["approved"] is True and "needs_approval" not in done
    assert done["undo"] == (
        f"history(doc_id='{doc}', action='restore', snapshot_id='{done['snapshot_id']}')"
    )
    assert 'id="r1"' not in working_svg(doc)
    assert done["removed"] == ["r1"] and "reason" not in done
    low = _edit(tool_app, doc, [RECT]).data
    assert "approved" not in low and "needs_approval" not in low


def test_dry_run_writes_nothing(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    before = working_svg(doc)
    plan = _edit(tool_app, doc, [RECT], dry_run=True).data
    assert plan["plan"][0]["creates"] == ["bg"]
    assert working_svg(doc) == before
    assert (
        "snapshots" not in call(tool_app, "inspect", {"doc_id": doc, "include": ["snapshots"]}).data
    )


def test_render_inline_attaches_exactly_one_png(tool_app, tool_ws, tool_renders) -> None:
    doc = open_doc(tool_ws)
    res = _edit(tool_app, doc, [RECT], render="inline")
    assert len(res.images) == 1 and res.images[0].mimeType == "image/png"
    assert tool_renders == [doc]


def test_default_edit_renders_nothing(tool_app, tool_ws, tool_renders) -> None:
    doc = open_doc(tool_ws)
    res = _edit(tool_app, doc, [RECT])
    assert res.images == []
    assert tool_renders == []


def test_render_failure_does_not_undo_the_edit(tool_app, tool_ws, monkeypatch) -> None:
    from inkscape_mcp.render.cli import RenderError

    def boom(*_a, **_k):
        raise RenderError("no inkscape")

    monkeypatch.setattr("inkscape_mcp.edit.ops.render_preview", boom)
    doc = open_doc(tool_ws)
    res = _edit(tool_app, doc, [RECT], render="inline")
    assert res.data["changed"] is True
    assert res.images == []
    assert any("render unavailable" in w for w in res.data["warnings"])


def test_unknown_document(tool_app) -> None:
    with pytest.raises(ToolError, match="document id not found"):
        _edit(tool_app, "nope", [RECT])


def test_a_noop_edit_reports_unchanged(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    data = _edit(tool_app, doc, [{"op": "fill", "ids": ["r1"], "color": "#ff0000"}]).data
    assert data["changed"] is False
    assert "snapshot_id" not in data
    assert data["reason"].startswith(
        "nothing changed: fill: set fill on 1 object (already #ff0000: 'r1')"
    )


def test_document_wide_replace_color_shows_in_hits(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    _edit(tool_app, doc, [{**RECT, "fill": "#ff0000"}])  # + 2 red rects in the fixture
    ops = [{"op": "replace_color", "from_color": "#ff0000", "to_color": "#00ff00"}]
    assert _edit(tool_app, doc, ops).data["hits"] == [{"op": "replace_color", "matched": 3}]
    assert _edit(tool_app, doc, ops).data["hits"] == [{"op": "replace_color", "matched": 0}]


def test_hits_count_matched_objects_per_op(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    ops = [RECT, {"op": "fill", "select": {"tag": "rect"}, "color": "#f00"}]
    (hit,) = _edit(tool_app, doc, ops).data["hits"]
    assert hit["op"] == "fill" and hit["matched"] >= 2
    delete = [{"op": "delete", "ids": ["r1"]}]
    done = _edit(tool_app, doc, delete, approval_token="yes").data
    assert done["hits"] == [{"op": "delete", "matched": 1}]
