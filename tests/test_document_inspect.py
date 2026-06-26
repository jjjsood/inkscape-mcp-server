"""Inspection-engine tests (E1-04): models + functions over a fixture SVG."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.document.inspect import (
    DocumentNotFound,
    inspect_assets,
    inspect_fonts,
    inspect_layers,
    inspect_objects,
    inspect_styles,
    inspect_summary,
    inspect_tree,
)
from inkscape_mcp.registry import get_registry, reset_registry

FIXTURE_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
     xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.0.dtd"
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
def doc_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(root))
    get_settings.cache_clear()
    reset_registry()

    src = root / "fixture.svg"
    src.write_bytes(FIXTURE_SVG)

    entry = get_registry().open_document(str(src))
    return entry.doc_id


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


def test_original_source_byte_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(root))
    get_settings.cache_clear()
    reset_registry()

    src = root / "fixture.svg"
    src.write_bytes(FIXTURE_SVG)

    entry = get_registry().open_document(str(src))
    # Full inspection pass over the working copy.
    inspect_summary(entry.doc_id)
    inspect_tree(entry.doc_id)
    inspect_layers(entry.doc_id)
    inspect_objects(entry.doc_id)
    inspect_styles(entry.doc_id)
    inspect_fonts(entry.doc_id)
    inspect_assets(entry.doc_id)

    # The ORIGINAL source file is byte-for-byte unchanged.
    assert src.read_bytes() == FIXTURE_SVG


# --- E10-06 / E11-06: paint, leaf/layer flags, bbox, font availability, used_by --------------

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
def paint_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(root))
    get_settings.cache_clear()
    reset_registry()
    src = root / "paint.svg"
    src.write_bytes(PAINT_SVG)
    return get_registry().open_document(str(src)).doc_id


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


def test_stroke_only_check_answerable_from_objects(paint_doc: str) -> None:
    """S13: 'no stroke-only element' is answerable from inspect_objects alone."""
    stroke_only = [o.id for o in inspect_objects(paint_doc).objects if o.paint.stroke_only]
    assert stroke_only == ["strokeonly"]


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


def test_leaf_only_check_answerable_from_objects(paint_doc: str) -> None:
    """S12: 'leaf objects only' is answerable — non-leaf object ids are enumerable."""
    non_leaf = sorted(o.id for o in inspect_objects(paint_doc).objects if not o.is_leaf)
    assert non_leaf == ["inner", "layer1"]


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
    # A transformed element is reported as None rather than an untransformed (wrong) box.
    assert _obj(paint_doc, "nested").bbox is None


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


def test_tree_nodes_carry_bbox_from_attributes(paint_doc: str) -> None:
    """inspect_document's tree nodes carry an attribute-derived bbox (no disk access)."""
    nodes = _tree_nodes_by_id(paint_doc)

    rect = nodes["filled"]
    assert rect.bbox is not None  # type: ignore[union-attr]
    assert (rect.bbox.x, rect.bbox.y, rect.bbox.width, rect.bbox.height) == (  # type: ignore[union-attr]
        1.0,
        2.0,
        10.0,
        20.0,
    )

    circle = nodes["dot"]
    assert circle.bbox is not None  # type: ignore[union-attr]
    # circle bbox = (cx-r, cy-r, 2r, 2r)
    assert (circle.bbox.x, circle.bbox.y, circle.bbox.width, circle.bbox.height) == (  # type: ignore[union-attr]
        45.0,
        35.0,
        10.0,
        10.0,
    )


def test_tree_nodes_bbox_none_when_not_derivable(paint_doc: str) -> None:
    """A <path> and a transformed element report bbox=None in the tree (not a wrong box)."""
    nodes = _tree_nodes_by_id(paint_doc)
    assert nodes["p1"].bbox is None  # type: ignore[union-attr]
    assert nodes["nested"].bbox is None  # type: ignore[union-attr]


def test_tree_bbox_matches_objects_bbox_parity(paint_doc: str) -> None:
    """Tree bbox and the objects-resource bbox are identical for every element (parity)."""
    nodes = _tree_nodes_by_id(paint_doc)
    objects = {o.id: o for o in inspect_objects(paint_doc).objects if o.id}
    # Every object id is present in the tree and carries the same bbox value.
    for obj_id, obj in objects.items():
        assert obj_id in nodes
        assert nodes[obj_id].bbox == obj.bbox  # type: ignore[union-attr]


def test_where_is_element_answerable_from_tree_alone(paint_doc: str) -> None:
    """An agent can answer 'where is element X' from inspect_tree alone — no disk read."""
    nodes = _tree_nodes_by_id(paint_doc)
    box = nodes["filled"].bbox  # type: ignore[union-attr]
    assert box is not None
    # Concrete placement answer: top-left corner and extent, in user units.
    assert (box.x, box.y) == (1.0, 2.0)
    assert (box.width, box.height) == (10.0, 20.0)


def test_fonts_available_flag_and_used_by(paint_doc: str) -> None:
    fonts = {f.family: f for f in inspect_fonts(paint_doc).fonts}
    assert "NoSuchFontXYZ123" in fonts
    bogus = fonts["NoSuchFontXYZ123"]
    # used_by points at the referencing element id (D8/R7 + E11-10c).
    assert bogus.used_by == "label"
    if shutil.which("fc-list") is None:
        # No fontconfig: availability is unknown (skipped), not falsely "missing".
        assert bogus.available is None
    else:
        assert bogus.available is False


def test_fonts_generic_keyword_always_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(root))
    get_settings.cache_clear()
    reset_registry()
    svg = (
        b'<?xml version="1.0"?>'
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        b'<text id="t" font-family="sans-serif">x</text></svg>'
    )
    src = root / "g.svg"
    src.write_bytes(svg)
    doc_id = get_registry().open_document(str(src)).doc_id
    fonts = {f.family: f for f in inspect_fonts(doc_id).fonts}
    # Generic CSS keywords always resolve, regardless of the font database.
    assert fonts["sans-serif"].available is True


def test_assets_carry_used_by(paint_doc: str) -> None:
    assets = {a.href: a for a in inspect_assets(paint_doc).assets}
    assert assets["external.png"].used_by == "pic"
