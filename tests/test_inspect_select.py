"""Object selection: the `find_objects` engine, `find_in_root`, and `inspect(select=...)`.

Covers every filter (tag, fill, stroke, text, id_prefix, bbox), AND-combination, CSS cascade and
inherited paint matching, and the opt-in engine bbox (needs Inkscape, marked accordingly).
"""

from __future__ import annotations

import pytest
from lxml import etree

from inkscape_mcp.document.inspect import BBox, find_in_root, find_objects
from tests.tool_support import call, open_doc

# Mixed document the agent did NOT author: rects (varied paint), an ellipse, text, a path (no
# analytic bbox), a polygon, and one id-less rect (must never be returned).
MIXED = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">
  <rect id="blue1" x="0" y="0" width="20" height="20" style="fill:#3366CC"/>
  <rect id="blue2" x="100" y="100" width="20" height="20" fill="#3366cc" stroke="#000000"/>
  <rect id="red1" x="50" y="50" width="10" height="10" fill="red"/>
  <rect x="5" y="5" width="3" height="3" fill="#3366cc"/>
  <ellipse id="el1" cx="150" cy="20" rx="10" ry="5" fill="#ffffff"/>
  <text id="title" x="10" y="180" font-family="Arial">Total: 42 widgets</text>
  <path id="p1" d="M0 0 L10 10" stroke="#00ff00"/>
  <polygon id="poly1" points="0,0 10,0 10,10" fill="#abcdef"/>
</svg>
"""
ALL_IDS = {"blue1", "blue2", "red1", "el1", "title", "p1", "poly1"}

# Paint from a <style> rule (element / class / id selectors) and from an ancestor <g>.
CSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">
  <style type="text/css">
    .brand { fill: #3366cc; }
    rect { stroke: #112233; }
    #special { fill: #ff8800; }
  </style>
  <rect id="byclass" class="brand" x="0" y="0" width="10" height="10"/>
  <rect id="byid" class="brand" x="20" y="0" width="10" height="10"/>
  <g id="inheritg" fill="#00cc66">
    <rect id="inherited" x="40" y="0" width="10" height="10"/>
    <rect id="ownpaint" x="60" y="0" width="10" height="10" fill="#abcdef"/>
  </g>
</svg>
"""

# A path inside a translated group: no analytic attribute bbox, so only the engine box locates it.
TRANSFORMED = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">
  <g id="grp" transform="translate(100,100)">
    <path id="tri" d="M0 0 L20 0 L20 20 Z" fill="#444444"/>
  </g>
</svg>
"""


def _ids(result) -> set[str]:
    return {o.object_id for o in result.objects}


@pytest.fixture
def mixed(tool_ws) -> str:
    return open_doc(tool_ws, "mixed.svg", MIXED)


@pytest.fixture
def css(tool_ws) -> str:
    return open_doc(tool_ws, "css.svg", CSS)


@pytest.fixture
def transformed(tool_ws) -> str:
    return open_doc(tool_ws, "tri.svg", TRANSFORMED)


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        pytest.param({"tag": "rect"}, {"blue1", "blue2", "red1"}, id="tag-skips-idless"),
        pytest.param({"fill": "#3366cc"}, {"blue1", "blue2"}, id="fill-case-insensitive"),
        pytest.param({"fill": "red"}, {"red1"}, id="fill-named-colour"),
        pytest.param({"stroke": "#000000"}, {"blue2"}, id="stroke"),
        pytest.param({"text": "total"}, {"title"}, id="text-substring-ci"),
        pytest.param({"text": "nope"}, set(), id="text-no-hit"),
        pytest.param({"id_prefix": "blue"}, {"blue1", "blue2"}, id="id-prefix"),
        pytest.param({"tag": "circle"}, set(), id="empty-result"),
        pytest.param({}, ALL_IDS, id="no-filters-all-addressable"),
        pytest.param(
            {"bbox": BBox(x=0, y=0, width=5, height=5)}, {"blue1", "poly1"}, id="bbox-corner"
        ),
        pytest.param(
            {"tag": "rect", "fill": "#3366cc", "bbox": BBox(x=0, y=0, width=10, height=10)},
            {"blue1"},
            id="filters-are-anded",
        ),
    ],
)
def test_find_objects_filters(mixed, filters, expected) -> None:
    result = find_objects(mixed, **filters)
    assert _ids(result) == expected
    assert result.count == len(expected)


def test_bbox_filter_excludes_objects_without_an_analytic_box(mixed) -> None:
    hits = _ids(find_objects(mixed, bbox=BBox(x=-10, y=-10, width=500, height=500)))
    assert "p1" not in hits
    assert {"blue1", "blue2", "red1", "el1", "poly1"} <= hits


def test_object_ref_carries_bbox_and_paint(mixed) -> None:
    [poly] = find_objects(mixed, id_prefix="poly1").objects
    assert (poly.tag, poly.fill) == ("polygon", "#abcdef")
    assert (poly.bbox.x, poly.bbox.y, poly.bbox.width, poly.bbox.height) == (0, 0, 10, 10)


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        pytest.param({"fill": "#3366cc"}, {"byclass", "byid"}, id="class-rule"),
        pytest.param(
            {"stroke": "#112233"}, {"byclass", "byid", "inherited", "ownpaint"}, id="element-rule"
        ),
        pytest.param({"tag": "rect", "fill": "#00cc66"}, {"inherited"}, id="inherited-from-group"),
        pytest.param({"fill": "#abcdef"}, {"ownpaint"}, id="own-paint-beats-inheritance"),
    ],
)
def test_matching_uses_the_css_cascade(css, filters, expected) -> None:
    assert _ids(find_objects(css, **filters)) == expected


def test_reported_fill_stays_the_per_element_token(css) -> None:
    """The cascade is for MATCHING only; class/inherited-only objects report fill=None."""
    fills = {o.object_id: o.fill for o in find_objects(css).objects}
    assert fills["byclass"] is None and fills["inherited"] is None
    assert fills["ownpaint"] == "#abcdef"


def test_transformed_path_has_no_attribute_bbox(transformed) -> None:
    [tri] = find_objects(transformed, id_prefix="tri").objects
    assert tri.bbox is None


@pytest.mark.inkscape
def test_accurate_bbox_locates_the_transformed_path(transformed) -> None:
    [tri] = find_objects(transformed, id_prefix="tri", accurate_bbox=True).objects
    assert tri.bbox.x == pytest.approx(100, abs=2) and tri.bbox.y == pytest.approx(100, abs=2)
    assert tri.bbox.width == pytest.approx(20, abs=2)
    box = BBox(x=100, y=100, width=10, height=10)
    assert "tri" in _ids(find_objects(transformed, bbox=box, accurate_bbox=True))
    assert "tri" not in _ids(find_objects(transformed, bbox=box))


@pytest.mark.inkscape
def test_select_accurate_bbox_through_the_tool(tool_app, transformed) -> None:
    select = {"id_prefix": "tri", "accurate_bbox": True}
    rows = call(tool_app, "inspect", {"doc_id": transformed, "select": select}).data["objects"]
    assert rows["objects"][0][2] is not None


def test_find_in_root_filters_an_in_memory_tree() -> None:
    """Objects created earlier in the same edit call are findable before anything is on disk."""
    root = etree.fromstring(
        b'<svg xmlns="http://www.w3.org/2000/svg">'
        b'<rect id="a" width="5" height="5" fill="#FF0000"/>'
        b'<circle id="b" r="3" fill="blue"/></svg>'
    )
    assert [o.object_id for o in find_in_root(root, fill="#f00")] == ["a"]
    root[1].set("id", "renamed")
    assert [o.object_id for o in find_in_root(root, tag="circle")] == ["renamed"]


def test_text_filter_matches_only_the_outermost_text_element() -> None:
    root = etree.fromstring(
        b'<svg xmlns="http://www.w3.org/2000/svg"><g id="layer">'
        b'<text id="t"><tspan id="ts">Spring Sale</tspan></text><rect id="r"/></g></svg>'
    )
    assert {o.object_id for o in find_in_root(root, text="sale")} == {"t"}
    assert find_in_root(root, text="autumn") == []


def test_find_in_root_engine_boxes_override_attribute_boxes() -> None:
    root = etree.fromstring(
        b'<svg xmlns="http://www.w3.org/2000/svg"><rect id="a" width="5" height="5"/></svg>'
    )
    box = BBox(x=1.0, y=2.0, width=3.0, height=4.0)
    (ref,) = find_in_root(root, engine_boxes={"a": box})
    assert ref.bbox == box


# --- the same selection through the `inspect` tool ---------------------------------------------


def _rows(app, doc, **args):
    return call(app, "inspect", {"doc_id": doc, **args}).data["objects"]


def test_select_passes_every_filter_through(tool_app, mixed) -> None:
    data = call(
        tool_app, "inspect", {"doc_id": mixed, "select": {"tag": "rect", "fill": "#3366CC"}}
    )
    assert {r[0] for r in data.data["objects"]["objects"]} == {"blue1", "blue2"}
    assert "summary" not in data.data  # select implies include=[objects] only
    by_bbox = _rows(tool_app, mixed, select={"bbox": {"x": 0, "y": 0, "width": 5, "height": 5}})
    assert {r[0] for r in by_bbox["objects"]} == {"blue1", "poly1"}


def test_object_rows_for_a_mixed_document(tool_app, mixed) -> None:
    objects = _rows(tool_app, mixed, include=["objects"])
    assert objects["count"] == len(ALL_IDS)  # the id-less rect never appears
    assert objects["columns"] == ["id", "tag", "bbox", "fill"]
    rows = {r[0]: r for r in objects["objects"]}
    assert set(rows) == ALL_IDS
    assert rows["blue1"][1] == "rect"
    assert rows["blue1"][2] == [0, 0, 20, 20]
    assert rows["blue1"][3] == "#3366CC"
    assert rows["p1"][2] is None  # a path has no analytic box
    empty = _rows(tool_app, mixed, select={"tag": "circle"})
    assert empty == {"count": 0, "columns": ["id", "tag", "bbox", "fill"]}
