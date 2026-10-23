"""`inspect`: sections, selectors, compact rows, truncation, document sets."""

from __future__ import annotations

from pathlib import Path

from tests.tool_support import call, open_doc

LAYERED = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
     width="10" height="10" viewBox="0 0 10 10">
  <g id="l1" inkscape:groupmode="layer" inkscape:label="L1">
    <rect id="r1" x="2" y="3" width="4" height="5" style="fill:#ff0000"/>
  </g>
</svg>
"""


def _walk(node):
    yield node
    for child in node.get("children", []):
        yield from _walk(child)


def test_default_is_the_summary(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    data = call(tool_app, "inspect", {"doc_id": doc}).data
    assert set(data) == {"doc_id", "summary"}
    assert data["summary"]["num_objects"] >= 3


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
    # plain ids (r1, t1) and a property some docs lack: exercises the consistency fallbacks
    a = open_doc(tool_ws, "a.svg")
    b = open_doc(tool_ws, "b.svg")
    data = call(tool_app, "inspect", {"doc_id": [a, b], "include": ["quality"]}).data
    assert set(data["docs"]) == {a, b}
    assert "consistency" in data


def test_layers_section_lists_the_layer_labels(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws, "a.svg", LAYERED)
    layers = call(tool_app, "inspect", {"doc_id": doc, "include": ["layers"]}).data["layers"]
    assert [layer["label"] for layer in layers["layers"]] == ["L1"]


def test_tree_carries_per_element_bbox(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws, "a.svg", LAYERED)
    tree = call(tool_app, "inspect", {"doc_id": doc, "include": ["tree"]}).data["tree"]
    rect = next(n for n in _walk(tree["root"]) if n.get("id") == "r1")
    assert rect["bbox"] == {"x": 2.0, "y": 3.0, "width": 4.0, "height": 5.0}


def test_validate_section_flags_a_broken_document(tool_app, tool_ws) -> None:
    bad = LAYERED.replace(b'style="fill:#ff0000"', b'fill="url(#nope)"')
    doc = open_doc(tool_ws, "bad.svg", bad)
    report = call(tool_app, "inspect", {"doc_id": doc, "include": ["validate"]}).data["validate"]
    assert report["ok"] is False
    assert [f["code"] for f in report["findings"]] == ["missing_id"]


def test_summary_object_count_matches_object_list(tool_app, tool_ws) -> None:
    poster = (Path(__file__).parent / "e2e" / "fixtures" / "brand_poster.svg").read_bytes()
    doc = open_doc(tool_ws, "poster.svg", poster)
    res = call(tool_app, "inspect", {"doc_id": doc, "include": ["summary", "objects"]}).data
    assert res["summary"]["num_objects"] == res["objects"]["count"] == 23
