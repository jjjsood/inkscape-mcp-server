"""Inspection-engine tests: models + functions over a fixture SVG."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from lxml import etree

from inkscape_mcp.document.inspect import (
    DocumentNotFound,
    bbox_of,
    inspect_assets,
    inspect_fonts,
    inspect_layers,
    inspect_objects,
    inspect_styles,
    inspect_summary,
    inspect_tree,
)
from tests.tool_support import open_doc

FIXTURE_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
     xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
     xmlns:xlink="http://www.w3.org/1999/xlink"
     width="100mm" height="50mm" viewBox="0 0 100 50"
     inkscape:document-units="mm">
  <defs>
    <style type="text/css">.a { fill: #112233; } .b { stroke: blue; }</style>
  </defs>
  <g id="layer1" inkscape:groupmode="layer" inkscape:label="L1">
    <rect id="r1" x="0" y="0" width="10" height="10"
          style="fill:#ff0000;stroke:#00ff00"/>
    <text id="t1" x="5" y="5" font-family="Arial">hello</text>
    <image id="img1" x="0" y="0" width="10" height="10"
           xlink:href="external.png"/>
  </g>
  <g id="layer2" inkscape:groupmode="layer" inkscape:label="Hidden"
     style="display:none" sodipodi:insensitive="true"/>
</svg>
"""


@pytest.fixture
def doc_id(tool_ws: Path) -> str:
    return open_doc(tool_ws, "fixture.svg", FIXTURE_SVG)


def test_summary(doc_id: str) -> None:
    s = inspect_summary(doc_id)
    assert s.doc_id == doc_id
    assert s.width == "100mm"
    assert s.height == "50mm"
    assert s.units == "mm"
    assert s.viewbox == [0.0, 0.0, 100.0, 50.0]
    assert s.page_count == 1
    assert s.num_layers == 2
    assert s.root_tag == "svg"
    assert s.num_objects >= 4  # 2 layers + rect + text + image


def test_tree(doc_id: str) -> None:
    t = inspect_tree(doc_id)
    assert t.root.tag == "svg"
    # The first layer node is reachable and carries its label.
    labels = {node.label for node in t.root.children}
    # children of <svg> are <defs> and the two <g> layers
    child_tags = [node.tag for node in t.root.children]
    assert "defs" in child_tags
    assert child_tags.count("g") == 2
    assert "L1" in labels


def test_layers(doc_id: str) -> None:
    layers = inspect_layers(doc_id)
    by_label = {layer.label: layer for layer in layers.layers}
    assert "L1" in by_label
    l1 = by_label["L1"]
    assert l1.id == "layer1"
    assert l1.visible is True
    assert l1.locked is False
    assert l1.num_children == 3
    hidden = by_label["Hidden"]
    assert hidden.visible is False
    assert hidden.locked is True


def test_objects(doc_id: str) -> None:
    objs = inspect_objects(doc_id)
    tags = [o.tag for o in objs.objects]
    assert "rect" in tags
    assert "text" in tags
    assert "image" in tags
    assert "g" in tags
    # defs/style/svg are NOT objects
    assert "defs" not in tags
    assert "style" not in tags
    assert "svg" not in tags
    rect = next(o for o in objs.objects if o.id == "r1")
    assert rect.has_style is True


def test_styles(doc_id: str) -> None:
    styles = inspect_styles(doc_id)
    assert "#ff0000" in styles.colors
    assert "#00ff00" in styles.colors
    assert styles.inline_style_count >= 1
    assert styles.css_rule_count == 2  # .a and .b


def test_fonts(doc_id: str) -> None:
    fonts = inspect_fonts(doc_id)
    families = {f.family for f in fonts.fonts}
    assert "Arial" in families


def test_assets_external(doc_id: str) -> None:
    assets = inspect_assets(doc_id)
    images = [a for a in assets.assets if a.kind == "image"]
    assert len(images) == 1
    assert images[0].href == "external.png"
    assert images[0].external is True


def test_unknown_id_raises(doc_id: str) -> None:
    with pytest.raises(DocumentNotFound):
        inspect_summary("d_doesnotexist")


# --- /: paint, leaf/layer flags, bbox, font availability, used_by --------------

# A fixture exercising the per-element read surface: a stroke-only rect (S13), a leaf vs. a
# nested group (S12), geometry-bearing shapes (bbox), and a referenced font + asset (used_by).
PAINT_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
     xmlns:xlink="http://www.w3.org/1999/xlink"
     width="100" height="100" viewBox="0 0 100 100">
  <g id="layer1" inkscape:groupmode="layer" inkscape:label="L1">
    <rect id="filled" x="1" y="2" width="10" height="20" style="fill:#ff0000"/>
    <rect id="strokeonly" x="0" y="0" width="5" height="5"
          style="fill:none;stroke:#00ff00;stroke-width:2"/>
    <circle id="dot" cx="50" cy="40" r="5" fill="blue"/>
    <g id="inner">
      <rect id="nested" x="0" y="0" width="3" height="3" transform="translate(10,10)"/>
    </g>
    <path id="p1" d="M0 0 L10 10" style="stroke:#000"/>
    <text id="label" x="5" y="5" font-family="NoSuchFontXYZ123">hi</text>
    <image id="pic" x="0" y="0" width="4" height="4" xlink:href="external.png"/>
  </g>
</svg>
"""


@pytest.fixture
def paint_doc(tool_ws: Path) -> str:
    return open_doc(tool_ws, "paint.svg", PAINT_SVG)


def _obj(doc_id: str, obj_id: str):
    return next(o for o in inspect_objects(doc_id).objects if o.id == obj_id)


def test_objects_carry_paint_summary(paint_doc: str) -> None:
    filled = _obj(paint_doc, "filled")
    assert filled.paint.fill == "#ff0000"
    assert filled.paint.stroke is None
    assert filled.paint.stroke_only is False

    stroke = _obj(paint_doc, "strokeonly")
    # Inline style is parsed into effective paint without reading the SVG off disk.
    assert stroke.paint.fill == "none"
    assert stroke.paint.stroke == "#00ff00"
    assert stroke.paint.stroke_width == "2"
    assert stroke.paint.stroke_only is True

    # Presentation-attribute paint (no inline style) is picked up too.
    dot = _obj(paint_doc, "dot")
    assert dot.paint.fill == "blue"

    # S13: "no stroke-only element" is answerable from inspect_objects alone.
    assert [o.id for o in inspect_objects(paint_doc).objects if o.paint.stroke_only] == [
        "strokeonly"
    ]


def test_is_leaf_and_is_layer_flags(paint_doc: str) -> None:
    objs = {o.id: o for o in inspect_objects(paint_doc).objects}
    # The layer group is a layer and is not a leaf (it has children).
    assert objs["layer1"].is_layer is True
    assert objs["layer1"].is_leaf is False
    # A plain group with children: not a layer, not a leaf.
    assert objs["inner"].is_layer is False
    assert objs["inner"].is_leaf is False
    # A drawable shape with no element children is a leaf.
    assert objs["filled"].is_leaf is True
    assert objs["filled"].is_layer is False
    # S12: "leaf objects only" is answerable: the non-leaf ids are enumerable.
    assert sorted(i for i, o in objs.items() if not o.is_leaf) == ["inner", "layer1"]


def test_objects_bbox_from_attributes(paint_doc: str) -> None:
    rect = _obj(paint_doc, "filled")
    assert rect.bbox is not None
    assert (rect.bbox.x, rect.bbox.y, rect.bbox.width, rect.bbox.height) == (1.0, 2.0, 10.0, 20.0)

    circle = _obj(paint_doc, "dot")
    assert circle.bbox is not None
    # circle bbox = (cx-r, cy-r, 2r, 2r)
    assert (circle.bbox.x, circle.bbox.y, circle.bbox.width, circle.bbox.height) == (
        45.0,
        35.0,
        10.0,
        10.0,
    )

    image = _obj(paint_doc, "pic")
    assert image.bbox is not None
    assert image.bbox.width == 4.0


def test_objects_bbox_none_when_not_derivable(paint_doc: str) -> None:
    # A <path> has no attribute-derived box (needs the engine).
    assert _obj(paint_doc, "p1").bbox is None
    # The element's own transform is applied (box in its parent's user space).
    nested = _obj(paint_doc, "nested").bbox
    assert nested is not None
    assert (nested.x, nested.y, nested.width, nested.height) == (10, 10, 3, 3)


_SVG = 'xmlns="http://www.w3.org/2000/svg"'


@pytest.mark.parametrize(
    ("elem", "box"),
    [
        # What the edit ops write: move then scale about the centre.
        (f'<rect {_SVG} x="10" y="10" width="20" height="10" transform="translate(5,0) '
         'translate(5,0) matrix(2,0,0,2,-20,-15)"/>', (10, 5, 40, 20)),
        (f'<circle {_SVG} cx="100" cy="50" r="25" transform="matrix(1.3,0,0,1.3,-30,-15)"/>',
         (67.5, 17.5, 65, 65)),
        (f'<rect {_SVG} x="0" y="0" width="20" height="10" transform="rotate(90,10,5)"/>',
         (5, -5, 10, 20)),
        (f'<rect {_SVG} width="10" height="10" transform="skewX(45)"/>', (0, 0, 20, 10)),
        (f'<line {_SVG} x1="0" y1="0" x2="10" y2="0" transform="rotate(90)"/>', (0, 0, 0, 10)),
        (f'<polygon {_SVG} points="0,0 4,0 4,2" transform="scale(2 3)"/>', (0, 0, 8, 6)),
        (f'<ellipse {_SVG} cx="0" cy="0" rx="2" ry="1" transform="scale(-1)"/>', (-2, -1, 4, 2)),
        # A rotated curve's corner box would be too big; malformed transforms are not guessed.
        (f'<circle {_SVG} cx="0" cy="0" r="5" transform="rotate(30)"/>', None),
        (f'<rect {_SVG} width="1" height="1" transform="wobble(2)"/>', None),
        (f'<rect {_SVG} width="1" height="1" transform="translate(1,"/>', None),
    ],
)  # fmt: skip
def test_bbox_applies_the_elements_own_transform(elem: str, box: tuple | None) -> None:
    got = bbox_of(etree.fromstring(elem))
    if box is None:
        assert got is None
    else:
        assert got is not None
        assert (got.x, got.y, got.width, got.height) == pytest.approx(box)


def test_tree_nodes_carry_paint_and_flags(paint_doc: str) -> None:
    tree = inspect_tree(paint_doc)
    found: dict[str, object] = {}

    def walk(node) -> None:
        if node.id:
            found[node.id] = node
        for child in node.children:
            walk(child)

    walk(tree.root)
    assert found["strokeonly"].paint.stroke_only is True  # type: ignore[union-attr]
    assert found["layer1"].is_layer is True  # type: ignore[union-attr]
    assert found["filled"].is_leaf is True  # type: ignore[union-attr]


def _tree_nodes_by_id(doc_id: str) -> dict[str, object]:
    """Flatten the inspect_tree result into an id -> node map for assertions."""
    found: dict[str, object] = {}

    def walk(node) -> None:
        if node.id:
            found[node.id] = node
        for child in node.children:
            walk(child)

    walk(inspect_tree(doc_id).root)
    return found


def test_tree_bbox_matches_objects_bbox_parity(paint_doc: str) -> None:
    """Tree bbox and the objects-resource bbox are identical for every element (parity)."""
    nodes = _tree_nodes_by_id(paint_doc)
    objects = {o.id: o for o in inspect_objects(paint_doc).objects if o.id}
    # Every object id is present in the tree and carries the same bbox value.
    for obj_id, obj in objects.items():
        assert obj_id in nodes
        assert nodes[obj_id].bbox == obj.bbox  # type: ignore[union-attr]


def test_fonts_available_flag_and_used_by(paint_doc: str) -> None:
    fonts = {f.family: f for f in inspect_fonts(paint_doc).fonts}
    assert "NoSuchFontXYZ123" in fonts
    bogus = fonts["NoSuchFontXYZ123"]
    # used_by points at the referencing element id (D8/R7 +).
    assert bogus.used_by == "label"
    if shutil.which("fc-list") is None:
        # No fontconfig: availability is unknown (skipped), not falsely "missing".
        assert bogus.available is None
    else:
        assert bogus.available is False


def test_fonts_generic_keyword_always_available(tool_ws: Path) -> None:
    svg = (
        b'<?xml version="1.0"?>'
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        b'<text id="t" font-family="sans-serif">x</text></svg>'
    )
    doc_id = open_doc(tool_ws, "g.svg", svg)
    fonts = {f.family: f for f in inspect_fonts(doc_id).fonts}
    # Generic CSS keywords always resolve, regardless of the font database.
    assert fonts["sans-serif"].available is True


def test_assets_carry_used_by(paint_doc: str) -> None:
    assets = {a.href: a for a in inspect_assets(paint_doc).assets}
    assert assets["external.png"].used_by == "pic"
