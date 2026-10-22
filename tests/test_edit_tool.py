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
    assert set(data) == {"doc_id", "changed", "snapshot_id", "risk", "created"}
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


def test_gradient_reference_in_a_paint(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    ops = [
        {
            "op": "linear_gradient",
            "id": "sky",
            "stops": [{"offset": 0, "color": "#fff"}, {"offset": 1, "color": "#00f"}],
        },
        {"op": "fill", "ids": ["r1"], "color": "url(#$0)"},
    ]
    assert _edit(tool_app, doc, ops).data["changed"] is True
    assert "url(#sky)" in working_svg(doc)


def test_one_call_is_one_snapshot(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    _edit(tool_app, doc, [RECT, {"op": "fill", "ids": ["r1"], "color": "#00f"}])
    snaps = call(tool_app, "inspect", {"doc_id": doc, "include": ["snapshots"]}).data["snapshots"]
    assert len(snaps) == 1


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
    with pytest.raises(ToolError, match="ops"):
        _edit(tool_app, doc, [{"op": "no_such_op"}])
    with pytest.raises(ToolError, match="at least one op"):
        _edit(tool_app, doc, [])


def test_high_risk_needs_approval_and_dry_run_reports_it(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    delete = {"op": "delete", "ids": ["r1"]}
    with pytest.raises(
        ToolError, match=r"ops\[0\] delete is HIGH risk; re-call with approval_token"
    ):
        _edit(tool_app, doc, [delete])
    plan = _edit(tool_app, doc, [delete], dry_run=True).data
    assert plan["dry_run"] is True and plan["needs_approval"] is True
    assert plan["changed"] is False
    assert plan["plan"][0]["matched"] == ["r1"]
    assert 'id="r1"' in working_svg(doc)
    done = _edit(tool_app, doc, [delete], approval_token="yes").data
    assert done["risk"] == "high" and done["changed"] is True
    assert 'id="r1"' not in working_svg(doc)


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


def test_path_ops_are_refused_when_raw_actions_are_disabled(tool_app, tool_ws, monkeypatch) -> None:
    from inkscape_mcp.config import ENV_RAW_ACTION_ENABLED, get_settings

    monkeypatch.setenv(ENV_RAW_ACTION_ENABLED, "0")
    get_settings.cache_clear()
    doc = open_doc(tool_ws)
    with pytest.raises(ToolError, match="path ops are disabled"):
        _edit(tool_app, doc, [{"op": "union", "ids": ["r1", "r2"]}], approval_token="x")
