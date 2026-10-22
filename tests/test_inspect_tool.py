"""`inspect`: sections, selectors, compact rows, truncation, document sets."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from tests.tool_support import call, open_doc


def test_default_is_the_summary(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    data = call(tool_app, "inspect", {"doc_id": doc}).data
    assert set(data) == {"doc_id", "summary"}
    assert data["summary"]["num_objects"] >= 3


def test_objects_are_compact_rows(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    data = call(tool_app, "inspect", {"doc_id": doc, "include": ["objects"]}).data
    objects = data["objects"]
    assert objects["columns"] == ["id", "tag", "bbox", "fill"]
    rows = {r[0]: r for r in objects["objects"]}
    assert rows["r1"][1] == "rect"
    assert rows["r1"][2] == [0, 0, 10, 10]
    assert rows["r1"][3] == "#ff0000"


def test_select_filters_and_adds_objects_to_include(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    data = call(tool_app, "inspect", {"doc_id": doc, "select": {"tag": "text"}}).data
    assert [r[0] for r in data["objects"]["objects"]] == ["t1"]
    assert "summary" not in data


def test_detail_ids_and_full(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    ids = call(tool_app, "inspect", {"doc_id": doc, "include": ["objects"], "detail": "ids"}).data[
        "objects"
    ]["objects"]
    assert {"r1", "r2", "t1"} <= set(ids)
    full = call(
        tool_app,
        "inspect",
        {"doc_id": doc, "include": ["objects"], "detail": "full", "select": {"id_prefix": "t"}},
    ).data["objects"]["objects"]
    assert full == [{"id": "t1", "tag": "text", "text": "hello"}]


def test_limit_truncates_and_flags_it(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    objects = call(tool_app, "inspect", {"doc_id": doc, "include": ["objects"], "limit": 1}).data[
        "objects"
    ]
    assert objects["truncated"] is True
    assert len(objects["objects"]) == 1
    assert objects["count"] >= 3


def test_other_sections(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    data = call(
        tool_app,
        "inspect",
        {
            "doc_id": doc,
            "include": [
                "tree",
                "layers",
                "styles",
                "fonts",
                "assets",
                "validate",
                "quality",
                "snapshots",
                "frames",
            ],
        },
    ).data
    for key in ("tree", "layers", "styles", "fonts", "assets", "validate", "quality"):
        assert key in data, key
    assert "ok" in data["validate"]


def test_snapshots_section_lists_edit_snapshots(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    call(tool_app, "edit", {"doc_id": doc, "ops": [{"op": "fill", "ids": ["r1"], "color": "#00f"}]})
    snaps = call(tool_app, "inspect", {"doc_id": doc, "include": ["snapshots"]}).data["snapshots"]
    assert len(snaps) == 1


def test_a_list_of_documents_is_keyed_by_id(tool_app, tool_ws) -> None:
    a = open_doc(tool_ws, "a.svg")
    b = open_doc(tool_ws, "b.svg")
    data = call(tool_app, "inspect", {"doc_id": [a, b], "include": ["quality"]}).data
    assert set(data["docs"]) == {a, b}
    assert "consistency" in data


def test_unknown_and_duplicate_ids_fail_before_any_work(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    with pytest.raises(ToolError, match="document id not found"):
        call(tool_app, "inspect", {"doc_id": [doc, "nope"]})
    with pytest.raises(ToolError):
        call(tool_app, "inspect", {"doc_id": [doc, doc], "include": ["quality"]})
    with pytest.raises(ToolError, match="at least one"):
        call(tool_app, "inspect", {"doc_id": []})


def test_inspect_writes_nothing(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    call(tool_app, "inspect", {"doc_id": doc, "include": ["objects", "validate", "quality"]})
    snaps = call(tool_app, "inspect", {"doc_id": doc, "include": ["snapshots"]}).data
    assert "snapshots" not in snaps  # empty list is dropped from the compact JSON
