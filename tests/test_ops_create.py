"""Create ops of `edit`: shapes, inline paint, gradients, grouping, `use`, rejections, undo.

Each case runs the op(s) through the kernel (`run_ops`) on a fixture SVG and inspects the on-disk
working copy. A rejected op must leave the working copy byte-identical. Hermetic: no op here
renders or calls Inkscape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from lxml import etree

from inkscape_mcp.document.inspect import SVG_NS, XLINK_NS
from inkscape_mcp.edit.dom import parse_style
from inkscape_mcp.edit.ops import OpError
from inkscape_mcp.edit.pipeline import EditApplyError
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.workspace import sandbox
from tests.conftest import run_ops
from tests.tool_support import open_doc

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" '
    b'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
    b'xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="100">'
    b'<g id="layer1" inkscape:groupmode="layer">'
    b'<rect id="r1" x="0" y="0" width="4" height="4"/>'
    b'<circle id="c1" cx="5" cy="5" r="2"/>'
    b"</g>"
    b"</svg>"
)

_SVG = f"{{{SVG_NS}}}"


@pytest.fixture
def doc(tool_ws: Path) -> str:
    return open_doc(tool_ws, "scene.svg", SVG)


def _working(root: Path, doc_id: str) -> bytes:
    return sandbox.working_copy(root, doc_id).read_bytes()


def _tree(root: Path, doc_id: str) -> etree._Element:
    return etree.fromstring(_working(root, doc_id))


def _find(tree: etree._Element, object_id: str) -> etree._Element:
    for elem in tree.iter():
        if isinstance(elem.tag, str) and elem.get("id") == object_id:
            return elem
    raise AssertionError(f"no element with id {object_id!r}")


def _created(tool_ws: Path, doc_id: str, op: dict[str, Any]) -> tuple[Any, etree._Element]:
    """Run one create op; return its result and the created element from the working copy."""
    result = run_ops(doc_id, op)
    assert result.changed is True
    return result, _find(_tree(tool_ws, doc_id), result.created[0].id)


def _rejected(tool_ws: Path, doc_id: str, *ops: dict[str, Any], match: str | None = None) -> None:
    before = _working(tool_ws, doc_id)
    with pytest.raises(OpError, match=match):
        run_ops(doc_id, *ops)
    assert _working(tool_ws, doc_id) == before


# --- shape primitives ---------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "tag", "bbox"),
    [
        (
            {"op": "create_rect", "x": 10, "y": 20, "width": 30, "height": 40},
            "rect",
            [10, 20, 30, 40],
        ),
        ({"op": "create_circle", "cx": 10, "cy": 10, "r": 5}, "circle", [5, 5, 10, 10]),
        ({"op": "create_ellipse", "cx": 10, "cy": 10, "rx": 4, "ry": 2}, "ellipse", [6, 8, 8, 4]),
        ({"op": "create_line", "x1": 1, "y1": 2, "x2": 9, "y2": 6}, "line", [1, 2, 8, 4]),
        (
            {"op": "create_polygon", "points": [[0, 0], [10, 0], [5, 8]]},
            "polygon",
            [0, 0, 10, 8],
        ),
        ({"op": "create_polyline", "points": [[2, 2], [4, 8]]}, "polyline", [2, 2, 2, 6]),
        # path and text have no attribute-derived box
        ({"op": "create_path", "d": "M0,0 L10,10 C 5 5 6 6 7 7 Z"}, "path", None),
        ({"op": "create_text", "x": 5, "y": 5, "text": "hi"}, "text", None),
    ],
)
def test_shape_is_created_with_tag_and_bbox(tool_ws, doc, op, tag, bbox) -> None:
    result, elem = _created(tool_ws, doc, op)
    assert elem.tag == f"{_SVG}{tag}"
    assert result.created[0].bbox == (None if bbox is None else [float(v) for v in bbox])
    assert result.snapshot_id and result.operation_id


def test_default_parent_is_the_first_layer_and_source_is_untouched(tool_ws, doc) -> None:
    _, rect = _created(tool_ws, doc, {"op": "create_rect", "x": 0, "y": 0, "width": 5, "height": 5})
    assert rect.getparent().get("id") == "layer1"
    assert (tool_ws / "scene.svg").read_bytes() == SVG


def test_explicit_parent_and_id(tool_ws, doc) -> None:
    op = {"op": "create_rect", "x": 0, "y": 0, "width": 5, "height": 5}
    result, rect = _created(tool_ws, doc, {**op, "parent": "layer1", "id": "mybox"})
    assert result.created[0].id == "mybox"
    assert rect.getparent().get("id") == "layer1"


def test_polygon_points_are_serialised(tool_ws, doc) -> None:
    _, poly = _created(tool_ws, doc, {"op": "create_polygon", "points": [[0, 0], [10, 0], [5, 8]]})
    assert poly.get("points") == "0,0 10,0 5,8"


def test_path_data_is_stored_verbatim(tool_ws, doc) -> None:
    d = "M0,0 L10,10 C 5 5 6 6 7 7 Z"
    _, path = _created(tool_ws, doc, {"op": "create_path", "d": d})
    assert path.get("d") == d


def test_text_is_stored_as_a_text_node_never_as_markup(tool_ws, doc) -> None:
    _, text = _created(tool_ws, doc, {"op": "create_text", "x": 5, "y": 5, "text": "Hi <b>x</b>"})
    assert text.text == "Hi <b>x</b>"
    assert len(text) == 0


# --- inline paint on create ----------------------------------------------------


def test_rect_inline_fill_stroke_and_width(tool_ws, doc) -> None:
    op = {"op": "create_rect", "x": 0, "y": 0, "width": 10, "height": 10}
    _, rect = _created(
        tool_ws, doc, {**op, "fill": "#3366cc", "stroke": "black", "stroke_width": "2"}
    )
    style = parse_style(rect)
    assert (style["fill"], style["stroke"], style["stroke-width"]) == ("#3366cc", "black", "2")


def test_circle_inline_fill_only(tool_ws, doc) -> None:
    _, circle = _created(
        tool_ws, doc, {"op": "create_circle", "cx": 10, "cy": 10, "r": 5, "fill": "red"}
    )
    assert parse_style(circle)["fill"] == "red"
    assert "stroke" not in parse_style(circle)


def test_text_inline_fill_keeps_content(tool_ws, doc) -> None:
    _, text = _created(
        tool_ws, doc, {"op": "create_text", "x": 5, "y": 5, "text": "hi", "fill": "#111"}
    )
    assert parse_style(text)["fill"] == "#111"
    assert text.text == "hi"


def test_line_inline_stroke_only(tool_ws, doc) -> None:
    _, line = _created(
        tool_ws,
        doc,
        {
            "op": "create_line",
            "x1": 0,
            "y1": 0,
            "x2": 9,
            "y2": 9,
            "stroke": "blue",
            "stroke_width": "3",
        },
    )
    style = parse_style(line)
    assert (style["stroke"], style["stroke-width"]) == ("blue", "3")


def test_no_paint_means_no_style_block(tool_ws, doc) -> None:
    _, rect = _created(tool_ws, doc, {"op": "create_rect", "x": 0, "y": 0, "width": 9, "height": 9})
    assert rect.get("style") in (None, "")


def test_inline_fill_accepts_url_paint(tool_ws, doc) -> None:
    op = {"op": "create_rect", "x": 0, "y": 0, "width": 9, "height": 9, "fill": "url(#grad)"}
    _, rect = _created(tool_ws, doc, op)
    assert parse_style(rect)["fill"] == "url(#grad)"


def test_inline_paint_rejects_a_bad_color(tool_ws, doc) -> None:
    _rejected(
        tool_ws,
        doc,
        {"op": "create_path", "d": "M0 0 L10 0", "fill": "#zzz; evil:1"},
        match=r"ops\[0\] create_path",
    )


# --- gradients ----------------------------------------------------------------


def test_linear_gradient_lands_in_defs_with_stops(tool_ws, doc) -> None:
    stops = [
        {"offset": "0", "color": "#ffffff"},
        {"offset": "100%", "color": "red", "opacity": 0.5},
    ]
    result, grad = _created(tool_ws, doc, {"op": "linear_gradient", "stops": stops})
    assert grad.tag == f"{_SVG}linearGradient"
    assert grad.getparent().tag == f"{_SVG}defs"
    first, second = grad.findall(f"{_SVG}stop")
    assert "stop-color:#ffffff" in first.get("style")
    assert "stop-opacity:0.5" in second.get("style")
    assert result.created[0].bbox is None


def test_radial_gradient_lands_in_defs(tool_ws, doc) -> None:
    stops = [{"offset": "0", "color": "blue"}, {"offset": "1", "color": "black"}]
    _, grad = _created(tool_ws, doc, {"op": "radial_gradient", "stops": stops, "fx": "40%"})
    assert grad.tag == f"{_SVG}radialGradient"
    assert grad.get("fx") == "40%"
    assert grad.getparent().tag == f"{_SVG}defs"


@pytest.mark.parametrize(
    "stop",
    [
        {"offset": "0", "color": "red;fill:url(#x){"},  # CSS injection
        {"offset": "2", "color": "red"},  # offset outside 0..1
    ],
)
def test_gradient_rejects_bad_stop(tool_ws, doc, stop) -> None:
    _rejected(tool_ws, doc, {"op": "linear_gradient", "stops": [stop]}, match="linear_gradient")


# --- grouping / reparent ------------------------------------------------------


def test_create_group_is_empty(tool_ws, doc) -> None:
    _, group = _created(tool_ws, doc, {"op": "create_group", "parent": "layer1"})
    assert group.tag == f"{_SVG}g"
    assert len(group) == 0


def test_group_wraps_existing_objects_in_place(tool_ws, doc) -> None:
    _, group = _created(tool_ws, doc, {"op": "group", "ids": ["r1", "c1"]})
    assert {c.get("id") for c in group} == {"r1", "c1"}
    assert group.getparent().get("id") == "layer1"
    ids = [
        e.get("id") for e in _tree(tool_ws, doc).iter() if isinstance(e.tag, str) and e.get("id")
    ]
    assert len(ids) == len(set(ids))


def test_group_accepts_a_selector(tool_ws, doc) -> None:
    _, group = _created(tool_ws, doc, {"op": "group", "select": {"tag": "circle"}, "id": "cg"})
    assert [c.get("id") for c in group] == ["c1"]


@pytest.mark.parametrize(
    ("op", "match"),
    [
        ({"op": "group", "ids": []}, "'ids' is empty"),
        ({"op": "group", "ids": ["nope"]}, "id 'nope' not found"),
        ({"op": "group", "select": {"tag": "polygon"}}, "select matched 0 objects"),
    ],
)
def test_group_rejects_empty_or_missing_targets(tool_ws, doc, op, match) -> None:
    _rejected(tool_ws, doc, op, match=match)


def test_reparent_moves_under_a_group_created_in_the_same_call(tool_ws, doc) -> None:
    run_ops(
        doc,
        {"op": "create_group", "parent": "layer1", "id": "bucket"},
        {"op": "reparent", "ids": ["r1"], "parent": "$0"},
    )
    assert _find(_tree(tool_ws, doc), "r1").getparent().get("id") == "bucket"


@pytest.mark.parametrize(
    ("op", "match"),
    [
        # layer1 contains r1; moving layer1 under r1 would loop
        ({"op": "reparent", "ids": ["layer1"], "parent": "r1"}, "descendant"),
        ({"op": "reparent", "ids": ["r1"], "parent": "nope"}, "parent id 'nope' not found"),
    ],
)
def test_reparent_rejections(tool_ws, doc, op, match) -> None:
    _rejected(tool_ws, doc, op, match=match)


# --- use -----------------------------------------------------------------------


def test_use_references_an_existing_object(tool_ws, doc) -> None:
    _, use = _created(tool_ws, doc, {"op": "create_use", "href": "r1", "x": 10, "y": 20})
    assert use.tag == f"{_SVG}use"
    assert use.get(f"{{{XLINK_NS}}}href") == "#r1" and use.get("href") == "#r1"
    assert (use.get("x"), use.get("y")) == ("10", "20")


def test_use_accepts_a_valid_transform(tool_ws, doc) -> None:
    op = {"op": "create_use", "href": "r1", "transform": "translate(10,0) scale(2)"}
    _, use = _created(tool_ws, doc, op)
    assert use.get("transform") == "translate(10,0) scale(2)"


@pytest.mark.parametrize(
    "href", ["https://evil.example/x.svg#a", "javascript:alert(1)", "does_not_exist"]
)
def test_use_rejects_external_or_missing_targets(tool_ws, doc, href) -> None:
    _rejected(tool_ws, doc, {"op": "create_use", "href": href}, match="create_use")


@pytest.mark.parametrize(
    "bad_transform",
    [
        "scale(2);fill:red",  # CSS-injection punctuation
        "evil(1)",  # function outside the SVG transform set
        "translate(10) <script>",  # markup
        "javascript:alert(1)",
    ],
)
def test_use_rejects_unsafe_transform(tool_ws, doc, bad_transform) -> None:
    op = {"op": "create_use", "href": "r1", "transform": bad_transform}
    _rejected(tool_ws, doc, op, match="create_use")


# --- shared rejections --------------------------------------------------------

RECT = {"op": "create_rect", "x": 0, "y": 0, "width": 5, "height": 5}


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"id": "bad id;<x>"}, "invalid object id"),
        ({"id": "r1"}, "id already in use"),
        ({"width": 0}, "greater than 0"),
        ({"parent": "nope"}, "parent id 'nope' not found"),
    ],
)
def test_create_rect_rejections(tool_ws, doc, patch, match) -> None:
    _rejected(tool_ws, doc, {**RECT, **patch}, match=match)


def test_create_path_rejects_markup_in_d(tool_ws, doc) -> None:
    d = 'M0,0 L10,10"/><script>alert(1)</script>'
    _rejected(tool_ws, doc, {"op": "create_path", "d": d}, match="invalid characters")


def test_a_failing_create_rolls_back_earlier_creates_of_the_call(tool_ws, doc) -> None:
    _rejected(tool_ws, doc, RECT, {**RECT, "id": "r1"}, match=r"ops\[1\] create_rect")


def test_unknown_document(doc) -> None:
    with pytest.raises(EditApplyError, match="document id not found"):
        run_ops("d_nope", RECT)


# --- reversibility ------------------------------------------------------------


def test_create_is_undone_by_its_snapshot(tool_ws, doc) -> None:
    before = _working(tool_ws, doc)
    result = run_ops(doc, {**RECT, "id": "new"})
    assert _working(tool_ws, doc) != before
    restore_snapshot(doc, result.snapshot_id)
    assert _working(tool_ws, doc) == before
