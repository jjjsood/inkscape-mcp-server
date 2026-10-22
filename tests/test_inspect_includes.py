"""`open` + `inspect(include=...)`: the document read side that the old open/inspect tools covered.

Sections come back keyed by name; path handling of `open` (workspace-relative anchoring, sandbox
rejection with no host path in the message) is exercised against the real registry.
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from tests.tool_support import call, open_doc, write_svg

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


def test_open_returns_id_and_summary(tool_app, tool_ws) -> None:
    write_svg(tool_ws, "a.svg", SVG)
    data = call(tool_app, "open", {"path": "a.svg"}).data
    assert data["doc_id"].startswith("d_")
    assert data["summary"]["layers"] == 1
    assert "working copy" in data["persist_hint"] and "save_as" in data["persist_hint"]


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


def test_unknown_id_is_a_tool_error(tool_app) -> None:
    with pytest.raises(ToolError, match="not found"):
        call(tool_app, "inspect", {"doc_id": "d_nope", "include": ["tree"]})


def test_validate_section_reports_a_clean_document(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws, "a.svg", SVG)
    report = call(tool_app, "inspect", {"doc_id": doc, "include": ["validate"]}).data["validate"]
    assert report["doc_id"] == doc and report["ok"] is True and report["error_count"] == 0


def test_validate_section_flags_a_broken_document(tool_app, tool_ws) -> None:
    bad = SVG.replace(b'style="fill:#ff0000"', b'fill="url(#nope)"')
    doc = open_doc(tool_ws, "bad.svg", bad)
    report = call(tool_app, "inspect", {"doc_id": doc, "include": ["validate"]}).data["validate"]
    assert report["ok"] is False
    assert [f["code"] for f in report["findings"]] == ["missing_id"]


# --- `open` path handling -----------------------------------------------------------------------


def test_open_relative_path_anchors_to_the_workspace_root(
    tool_app, tool_ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolved against the first root, NOT the process CWD."""
    (tool_ws / "e2e").mkdir()
    (tool_ws / "e2e" / "brandmark.svg").write_bytes(SVG)
    elsewhere = tool_ws.parent / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    data = call(tool_app, "open", {"path": "e2e/brandmark.svg"}).data
    assert data["doc_id"].startswith("d_")


def _outside(tool_ws, how: str) -> str:
    outside = tool_ws.parent / "outside.svg"
    outside.write_bytes(SVG)
    if how == "absolute":
        return str(outside)
    if how == "traversal":
        return "../outside.svg"
    (tool_ws / "link.svg").symlink_to(outside)
    return "link.svg"


@pytest.mark.parametrize("how", ["absolute", "traversal", "symlink"])
def test_open_outside_the_workspace_is_rejected_without_a_host_path(
    tool_app, tool_ws, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    monkeypatch.chdir(tool_ws)
    path = _outside(tool_ws, how)
    with pytest.raises(ToolError) as exc:
        call(tool_app, "open", {"path": path})
    assert "path rejected: outside workspace" in str(exc.value)
    assert str(tool_ws.parent) not in str(exc.value)
