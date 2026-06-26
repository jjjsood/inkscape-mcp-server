"""find_objects engine + tool tests (E14-07), plus the inspect_document addressable object list."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.document.inspect import BBox, find_objects
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.tools.document import inspect_document, open_document
from inkscape_mcp.tools.find import find_objects as find_objects_tool

inkscape_available = shutil.which("inkscape") is not None

# A mixed document the agent did NOT author: rects (varied paint), an ellipse, text, a path (no
# analytic bbox), a polygon, and one id-less rect (must never be returned).
FIXTURE_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     width="200" height="200" viewBox="0 0 200 200">
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


@pytest.fixture
def doc_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(root))
    get_settings.cache_clear()
    reset_registry()
    src = root / "fixture.svg"
    src.write_bytes(FIXTURE_SVG)
    return get_registry().open_document(str(src)).doc_id


def _ids(result) -> set[str]:
    return {o.object_id for o in result.objects}


def test_filter_tag(doc_id: str) -> None:
    result = find_objects(doc_id, tag="rect")
    # All id-bearing rects; the id-less rect is excluded.
    assert _ids(result) == {"blue1", "blue2", "red1"}
    assert result.count == 3


def test_filter_fill_case_and_shorthand_insensitive(doc_id: str) -> None:
    # #3366CC (style), #3366cc (attr) both match a requested #3366cc; the id-less one is dropped.
    result = find_objects(doc_id, fill="#3366cc")
    assert _ids(result) == {"blue1", "blue2"}


def test_filter_fill_named_color(doc_id: str) -> None:
    result = find_objects(doc_id, fill="red")
    assert _ids(result) == {"red1"}


def test_filter_stroke(doc_id: str) -> None:
    result = find_objects(doc_id, stroke="#000000")
    assert _ids(result) == {"blue2"}


def test_filter_text_substring_case_insensitive(doc_id: str) -> None:
    result = find_objects(doc_id, text="total")
    assert _ids(result) == {"title"}
    assert find_objects(doc_id, text="nope").count == 0


def test_filter_id_prefix(doc_id: str) -> None:
    result = find_objects(doc_id, id_prefix="blue")
    assert _ids(result) == {"blue1", "blue2"}


def test_filter_bbox_intersection(doc_id: str) -> None:
    # Box covering the top-left corner: hits blue1 (0,0,20,20) and poly1 (0,0,10,10).
    result = find_objects(doc_id, bbox=BBox(x=0, y=0, width=5, height=5))
    assert _ids(result) == {"blue1", "poly1"}


def test_filter_bbox_excludes_non_boxable(doc_id: str) -> None:
    # A huge box covering the whole canvas still excludes the path (bbox=None on direct DOM).
    result = find_objects(doc_id, bbox=BBox(x=-10, y=-10, width=500, height=500))
    assert "p1" not in _ids(result)
    # ...but the rects / ellipse / polygon (analytic bbox) are present.
    assert {"blue1", "blue2", "red1", "el1", "poly1"} <= _ids(result)


def test_combined_filters(doc_id: str) -> None:
    # tag + fill + bbox together (AND): only blue1 satisfies all three.
    result = find_objects(
        doc_id, tag="rect", fill="#3366cc", bbox=BBox(x=0, y=0, width=10, height=10)
    )
    assert _ids(result) == {"blue1"}


def test_no_filters_returns_all_addressable(doc_id: str) -> None:
    result = find_objects(doc_id)
    # Every id-bearing object; the lone id-less rect is excluded.
    assert _ids(result) == {"blue1", "blue2", "red1", "el1", "title", "p1", "poly1"}


def test_empty_result(doc_id: str) -> None:
    result = find_objects(doc_id, tag="circle")
    assert result.objects == []
    assert result.count == 0


def test_object_ref_carries_bbox_and_paint(doc_id: str) -> None:
    [poly] = find_objects(doc_id, id_prefix="poly1").objects
    assert poly.tag == "polygon"
    assert poly.fill == "#abcdef"
    assert poly.bbox is not None
    assert (poly.bbox.x, poly.bbox.y, poly.bbox.width, poly.bbox.height) == (0.0, 0.0, 10.0, 10.0)


def test_tool_unknown_doc_raises_toolerror(doc_id: str) -> None:
    with pytest.raises(ToolError) as exc:
        find_objects_tool("d_nope")
    assert str(exc.value) == "document id not found"


def test_tool_passes_filters_through(doc_id: str) -> None:
    result = find_objects_tool(doc_id, tag="rect", fill="#3366CC")
    assert _ids(result) == {"blue1", "blue2"}


def test_inspect_document_exposes_object_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ws2"
    root.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(root))
    get_settings.cache_clear()
    reset_registry()
    src = root / "fixture.svg"
    src.write_bytes(FIXTURE_SVG)
    opened = open_document(str(src))

    result = inspect_document(opened.doc_id)
    ids = {o.object_id for o in result.objects}
    assert {"blue1", "blue2", "red1", "el1", "title", "p1", "poly1"} == ids
    # The id-less rect never appears; refs carry paint/bbox like find_objects.
    by_id = {o.object_id: o for o in result.objects}
    assert by_id["blue1"].fill == "#3366CC"
    assert by_id["p1"].bbox is None


# --- E14-07b: CSS-cascade / <style>-rule / inherited paint matching ----------

# A document whose paint comes from a <style> rule (element / class / id selectors) and from
# inheritance off an ancestor <g>, NOT from inline style / presentation attrs on the leaf.
CSS_FIXTURE_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
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


@pytest.fixture
def css_doc_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    root = tmp_path / "wscss"
    root.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(root))
    get_settings.cache_clear()
    reset_registry()
    src = root / "css.svg"
    src.write_bytes(CSS_FIXTURE_SVG)
    return get_registry().open_document(str(src)).doc_id


def test_match_fill_from_css_class(css_doc_id: str) -> None:
    # `.brand { fill:#3366cc }` paints both class-bearing rects; the id rule overrides #special.
    result = find_objects(css_doc_id, fill="#3366cc")
    assert _ids(result) == {"byclass", "byid"}


def test_match_stroke_from_css_element_selector(css_doc_id: str) -> None:
    # `rect { stroke:#112233 }` applies to every rect in the document.
    result = find_objects(css_doc_id, stroke="#112233")
    assert _ids(result) == {"byclass", "byid", "inherited", "ownpaint"}


def test_match_fill_inherited_from_ancestor_group(css_doc_id: str) -> None:
    # `inherited` has no own fill -> takes the ancestor <g fill="#00cc66">; `ownpaint` overrides.
    # The group itself also carries that fill (its own value), so it matches too — the point of the
    # test is that the LEAF with no own paint is matched via inheritance.
    result = find_objects(css_doc_id, fill="#00cc66")
    assert "inherited" in _ids(result)
    assert "ownpaint" not in _ids(result)
    # Scoped to leaves only, just the inherited rect resolves to the ancestor fill.
    rects = find_objects(css_doc_id, tag="rect", fill="#00cc66")
    assert _ids(rects) == {"inherited"}


def test_match_fill_own_overrides_inheritance(css_doc_id: str) -> None:
    result = find_objects(css_doc_id, fill="#abcdef")
    assert _ids(result) == {"ownpaint"}


def test_reported_fill_stays_per_element_token(css_doc_id: str) -> None:
    # The cascade is used only for MATCHING; the REPORTED fill is the per-element authored token,
    # so a class/inherited-only object reports fill=None (back-compat for ObjectRef.fill).
    [byclass] = find_objects(css_doc_id, id_prefix="byclass").objects
    assert byclass.fill is None
    [inherited] = find_objects(css_doc_id, id_prefix="inherited").objects
    assert inherited.fill is None
    [ownpaint] = find_objects(css_doc_id, id_prefix="ownpaint").objects
    assert ownpaint.fill == "#abcdef"


# --- E14-07a: opt-in geometry-accurate (engine) bbox -------------------------

# A path inside a translated group: neither has an analytic attribute bbox, so the default
# attribute path yields bbox=None — the engine box is needed to locate/match them.
ENGINE_FIXTURE_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">
  <g id="grp" transform="translate(100,100)">
    <path id="tri" d="M0 0 L20 0 L20 20 Z" fill="#444444"/>
  </g>
</svg>
"""


@pytest.fixture
def engine_doc_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    root = tmp_path / "wseng"
    root.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(root))
    get_settings.cache_clear()
    reset_registry()
    src = root / "engine.svg"
    src.write_bytes(ENGINE_FIXTURE_SVG)
    return get_registry().open_document(str(src)).doc_id


def test_default_path_has_no_bbox_for_transformed_path(engine_doc_id: str) -> None:
    # Without accurate_bbox the transformed path / group report no attribute box.
    [tri] = find_objects(engine_doc_id, id_prefix="tri").objects
    assert tri.bbox is None


@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_accurate_bbox_reports_transformed_path_box(engine_doc_id: str) -> None:
    [tri] = find_objects(engine_doc_id, id_prefix="tri", accurate_bbox=True).objects
    assert tri.bbox is not None
    # translate(100,100) is applied: the engine box lands near (100,100), 20x20-ish.
    assert tri.bbox.x == pytest.approx(100, abs=2)
    assert tri.bbox.y == pytest.approx(100, abs=2)
    assert tri.bbox.width == pytest.approx(20, abs=2)
    assert tri.bbox.height == pytest.approx(20, abs=2)


@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_accurate_bbox_filter_matches_transformed_path(engine_doc_id: str) -> None:
    # A bbox filter over the translated region now hits the path (excluded on the attribute path).
    near = find_objects(
        engine_doc_id, bbox=BBox(x=100, y=100, width=10, height=10), accurate_bbox=True
    )
    assert "tri" in _ids(near)
    # The same filter without accurate_bbox excludes it (no attribute box).
    plain = find_objects(engine_doc_id, bbox=BBox(x=100, y=100, width=10, height=10))
    assert "tri" not in _ids(plain)


@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_accurate_bbox_tool_passthrough(engine_doc_id: str) -> None:
    result = find_objects_tool(engine_doc_id, id_prefix="tri", accurate_bbox=True)
    [tri] = result.objects
    assert tri.bbox is not None
