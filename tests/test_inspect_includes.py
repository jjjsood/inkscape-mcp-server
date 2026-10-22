"""`inspect(include=...)`: the document read side that the old inspect tools covered.

Sections come back keyed by name.
"""

from __future__ import annotations

from tests.tool_support import call, open_doc

SVG = b"""<?xml version="1.0"?>
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


def test_sections_aggregate_for_one_document(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws, "a.svg", SVG)
    data = call(
        tool_app, "inspect", {"doc_id": doc, "include": ["summary", "tree", "layers", "styles"]}
    ).data
    assert data["summary"]["num_objects"] >= 1 and data["doc_id"] == doc
    assert data["tree"]["root"]["tag"] == "svg"
    assert [layer["label"] for layer in data["layers"]["layers"]] == ["L1"]
    assert data["styles"]["colors"] == ["#ff0000"]


def test_tree_carries_per_element_bbox(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws, "a.svg", SVG)
    tree = call(tool_app, "inspect", {"doc_id": doc, "include": ["tree"]}).data["tree"]
    rect = next(n for n in _walk(tree["root"]) if n.get("id") == "r1")
    assert rect["bbox"] == {"x": 2.0, "y": 3.0, "width": 4.0, "height": 5.0}


def test_validate_section_flags_a_broken_document(tool_app, tool_ws) -> None:
    bad = SVG.replace(b'style="fill:#ff0000"', b'fill="url(#nope)"')
    doc = open_doc(tool_ws, "bad.svg", bad)
    report = call(tool_app, "inspect", {"doc_id": doc, "include": ["validate"]}).data["validate"]
    assert report["ok"] is False
    assert [f["code"] for f in report["findings"]] == ["missing_id"]
