"""`inspect`: sections, selectors, compact rows, truncation, document sets."""

from __future__ import annotations

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


def test_full_detail_shows_the_transform(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    call(tool_app, "edit", {"doc_id": doc, "ops": [{"op": "rotate", "ids": ["r1"], "degrees": 30}]})
    full = call(
        tool_app,
        "inspect",
        {"doc_id": doc, "include": ["objects"], "detail": "full", "select": {"id_prefix": "r1"}},
    ).data["objects"]["objects"]
    assert full[0]["transform"].startswith("rotate(30")


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
    assert data["tree"]["root"]["tag"] == "svg"
    assert data["styles"]["colors"] == ["#ff0000"]
    assert data["validate"]["ok"] is True and data["validate"]["error_count"] == 0
    assert data["quality"]["ok"] is True
    for key in ("layers", "fonts", "assets"):  # nothing to report: just the doc_id
        assert data[key] == {"doc_id": doc}, key
    assert data["frames"] == {"series": "run"}  # no frames rendered yet
    assert "snapshots" not in data  # empty list is dropped


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


def test_inspect_writes_nothing(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    call(tool_app, "inspect", {"doc_id": doc, "include": ["objects", "validate", "quality"]})
    snaps = call(tool_app, "inspect", {"doc_id": doc, "include": ["snapshots"]}).data
    assert "snapshots" not in snaps  # empty list is dropped from the compact JSON
