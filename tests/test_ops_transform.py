"""Transform ops of `edit`: move, scale, rotate, tile, resize_canvas (+bleed), normalize_viewbox,
fit_to_content.

Asserts the transform strings written to the working copy, honest `changed` (a no-op writes no
snapshot and no record), value validation with a byte-identical working copy on rejection, and
undo. `fit_to_content` needs the Inkscape engine: hermetic wiring tests fake the bbox query, the
real-binary ones carry the `inkscape` marker.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from lxml import etree

from inkscape_mcp.edit import transform as transform_engine
from inkscape_mcp.edit.ops import OpError
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.workspace import sandbox
from tests.conftest import run_ops
from tests.tool_support import open_doc

NS = "{http://www.w3.org/2000/svg}"

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" '
    b'viewBox="0 0 100 100"><rect id="r1" x="0" y="0" width="10" height="10"/></svg>'
)
NO_VIEWBOX = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40">'
    b'<rect id="r1" x="0" y="0" width="10" height="10"/></svg>'
)
#: content does not fill the page; the rect's bbox is "40 30 20 50"
OFFSET = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" '
    b'viewBox="0 0 200 200"><rect id="r1" x="40" y="30" width="20" height="50"/></svg>'
)


@pytest.fixture
def doc(tool_ws: Path) -> str:
    return open_doc(tool_ws, "logo.svg", SVG)


def _working(root: Path, doc_id: str) -> bytes:
    return sandbox.working_copy(root, doc_id).read_bytes()


def _svg(root: Path, doc_id: str) -> etree._Element:
    return etree.fromstring(_working(root, doc_id))


def _transform(root: Path, doc_id: str) -> str | None:
    return _svg(root, doc_id).find(f".//{NS}rect").get("transform")


def _counts(root: Path, doc_id: str) -> tuple[int, int]:
    """(snapshots, non-discarded operation records) written so far."""
    snaps = sandbox.snapshots_dir(root, doc_id)
    ops = sandbox.operations_dir(root, doc_id)
    records = [json.loads(p.read_text()) for p in ops.glob("op_*.json")] if ops.exists() else []
    return (
        len(list(snaps.glob("*.svg"))) if snaps.exists() else 0,
        sum(r["status"] != "discarded" for r in records),
    )


def _rejected(root: Path, doc_id: str, *ops: dict[str, Any], match: str | None = None) -> None:
    before, counts = _working(root, doc_id), _counts(root, doc_id)
    with pytest.raises(OpError, match=match):
        run_ops(doc_id, *ops)
    assert _working(root, doc_id) == before
    assert _counts(root, doc_id) == counts


def _assert_noop(root: Path, doc_id: str, result: Any, before: bytes, counts: Any) -> None:
    assert result.changed is False
    assert result.operation_id == "" and result.snapshot_id == ""
    assert _working(root, doc_id) == before
    assert _counts(root, doc_id) == counts


# --- move / scale / rotate ------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        ({"op": "move", "dx": 5, "dy": -3}, "translate(5,-3)"),
        ({"op": "move", "dx": 5}, "translate(5,0)"),
        # r1 is the 10x10 square at the origin: scale/rotate pivot on its centre (5,5).
        ({"op": "scale", "sx": 2}, "matrix(2,0,0,2,-5,-5)"),
        ({"op": "scale", "sx": 2, "sy": 3}, "matrix(2,0,0,3,-5,-10)"),
        ({"op": "rotate", "degrees": 45, "cx": 50, "cy": 50}, "rotate(45,50,50)"),
        ({"op": "rotate", "degrees": 90}, "rotate(90,5,5)"),
    ],
)
def test_transform_string_written_to_the_target(tool_ws, doc, op, expected) -> None:
    result = run_ops(doc, {**op, "ids": ["r1"]})
    assert result.changed is True
    assert _transform(tool_ws, doc) == expected
    assert (tool_ws / "logo.svg").read_bytes() == SVG


def test_scale_and_rotate_keep_an_already_transformed_object_in_place(tool_ws, doc) -> None:
    run_ops(doc, {"op": "move", "ids": ["r1"], "dx": 40, "dy": 0})
    run_ops(doc, {"op": "rotate", "ids": ["r1"], "degrees": 30})
    run_ops(doc, {"op": "scale", "ids": ["r1"], "sx": 0.5})
    # Own-space pivots are appended, so the earlier move still applies last.
    assert _transform(tool_ws, doc) == "translate(40,0) rotate(30,5,5) matrix(0.5,0,0,0.5,2.5,2.5)"


def test_curved_relative_path_pivots_on_its_control_point_box(tool_ws) -> None:
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        b'<path id="p" d="M10 10 c 5 5 10 10 20 0"/></svg>'
    )
    doc_id = open_doc(tool_ws, "curve.svg", svg)
    result = run_ops(doc_id, {"op": "rotate", "ids": ["p"], "degrees": 15})
    assert "about its centre" in result.outcomes[0].summary
    # points (10,10) (15,15) (20,20) (30,10) -> box 10..30 x 10..20 -> centre (20,15)
    assert _svg(tool_ws, doc_id).find(f".//{NS}path").get("transform") == "rotate(15,20,15)"


def test_unmeasurable_geometry_falls_back_to_the_origin(tool_ws) -> None:
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        b'<g id="g"><text>no position</text></g></svg>'
    )
    doc_id = open_doc(tool_ws, "text.svg", svg)
    ops = [{"op": "rotate", "ids": ["g"], "degrees": 15}, {"op": "scale", "ids": ["g"], "sx": 2}]
    result = run_ops(doc_id, *ops)
    assert "about the origin" in result.outcomes[0].summary
    assert _svg(tool_ws, doc_id).find(f".//{NS}g").get("transform") == "scale(2,2) rotate(15)"


@pytest.mark.parametrize(
    ("d", "box"),
    [
        ("M 0,-30 L 8,-8 L 30,0 Z", (0, -30, 30, 30)),
        ("m 10 10 h 20 v 5 l -5 5 z", (10, 10, 20, 10)),
        ("M0 0 Q 10 20 20 0 T 40 0", (0, 0, 40, 20)),
        ("M0 0 A 5 5 0 0 1 10 0", (0, -5, 10, 10)),
        ("M1e1 0L20 0", (10, 0, 10, 0)),
    ],
)
def test_path_points_box(d, box) -> None:
    from inkscape_mcp.edit.dom import path_points

    pts = path_points(d)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    assert (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)) == pytest.approx(box)


@pytest.mark.parametrize("bad", [0.0, -1.0, 1e-9, float("inf"), float("nan")])
def test_scale_rejects_invalid_factor(tool_ws, doc, bad) -> None:
    _rejected(tool_ws, doc, {"op": "scale", "ids": ["r1"], "sx": bad}, match=r"ops\[0\] scale")


def test_move_accepts_a_selector_and_several_ops_stack_on_one_snapshot(tool_ws, doc) -> None:
    run_ops(
        doc,
        {"op": "move", "select": {"tag": "rect"}, "dx": 1, "dy": 2},
        {"op": "rotate", "ids": ["r1"], "degrees": 10},
    )
    assert _transform(tool_ws, doc) == "translate(1,2) rotate(10,5,5)"
    assert _counts(tool_ws, doc) == (1, 1)


# --- tile ------------------------------------------------------------------------


def test_tile_builds_the_grid_in_one_reversible_call(tool_ws, doc) -> None:
    before = _working(tool_ws, doc)
    result = run_ops(doc, {"op": "tile", "ids": ["r1"], "rows": 4, "cols": 4, "dx": 20, "dy": 20})
    # the original plus 15 clones, all under ONE snapshot
    assert len(_svg(tool_ws, doc).findall(f".//{NS}rect")) == 16
    assert _counts(tool_ws, doc) == (1, 1)
    restore_snapshot(doc, result.snapshot_id)
    assert _working(tool_ws, doc) == before


def test_tile_offsets_clones_by_the_grid_step(tool_ws, doc) -> None:
    run_ops(doc, {"op": "tile", "ids": ["r1"], "rows": 2, "cols": 2, "dx": 25, "dy": 10})
    transforms = [r.get("transform") for r in _svg(tool_ws, doc).findall(f".//{NS}rect")]
    # the original stays put; each clone sits at (col * dx, row * dy)
    assert sorted(transforms, key=str) == sorted(
        [None, "translate(25,0)", "translate(0,10)", "translate(25,10)"], key=str
    )


def test_tile_1x1_is_a_noop(tool_ws, doc) -> None:
    before, counts = _working(tool_ws, doc), _counts(tool_ws, doc)
    result = run_ops(doc, {"op": "tile", "ids": ["r1"], "rows": 1, "cols": 1, "dx": 10, "dy": 10})
    _assert_noop(tool_ws, doc, result, before, counts)


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"rows": 0, "cols": 3}, "tile"),
        ({"rows": 100, "cols": 100}, "tile"),  # over the grid cap
    ],
)
def test_tile_rejections(tool_ws, doc, patch, match) -> None:
    op = {"op": "tile", "ids": ["r1"], "rows": 2, "cols": 2, "dx": 1, "dy": 1, **patch}
    _rejected(tool_ws, doc, op, match=match)


# --- resize_canvas -----------------------------------------------------------------


def test_resize_canvas_keeps_an_existing_viewbox(tool_ws, doc) -> None:
    run_ops(doc, {"op": "resize_canvas", "width": "200px", "height": "150px"})
    svg = _svg(tool_ws, doc)
    assert (svg.get("width"), svg.get("height")) == ("200px", "150px")
    assert svg.get("viewBox") == "0 0 100 100"


def test_resize_canvas_adjust_viewbox_retargets_and_is_reversible(tool_ws, doc) -> None:
    before = _working(tool_ws, doc)
    result = run_ops(
        doc, {"op": "resize_canvas", "width": "300px", "height": "120px", "adjust_viewbox": True}
    )
    svg = _svg(tool_ws, doc)
    assert (svg.get("width"), svg.get("height")) == ("300px", "120px")
    assert svg.get("viewBox") == "0 0 300 120"
    restore_snapshot(doc, result.snapshot_id)
    assert _working(tool_ws, doc) == before


def test_resize_canvas_rejects_an_injection_length(tool_ws, doc) -> None:
    _rejected(tool_ws, doc, {"op": "resize_canvas", "width": "10; evil", "height": "100"})


def _bleed_rects(root: Path, doc_id: str) -> list[etree._Element]:
    return [
        e
        for e in _svg(root, doc_id).iter(f"{NS}rect")
        if (e.get("id") or "").startswith("bleed-bg")
    ]


def test_bleed_grows_the_viewbox_and_paints_one_strip(tool_ws, doc) -> None:
    run_ops(
        doc,
        {
            "op": "resize_canvas",
            "width": "108",
            "height": "108",
            "bleed": 4,
            "bleed_color": "#ffffff",
        },
    )
    assert _svg(tool_ws, doc).get("viewBox") == "-4 -4 108 108"
    (bg,) = _bleed_rects(tool_ws, doc)
    assert bg.get("fill") == "#ffffff"
    assert [bg.get(k) for k in ("x", "y", "width", "height")] == ["-4", "-4", "108", "108"]


def test_without_bleed_there_is_no_background_rect(tool_ws, doc) -> None:
    run_ops(doc, {"op": "resize_canvas", "width": "200", "height": "200"})
    assert _svg(tool_ws, doc).get("viewBox") == "0 0 100 100"
    assert _bleed_rects(tool_ws, doc) == []


@pytest.mark.parametrize("patch", [{"adjust_viewbox": True, "bleed": 4}, {"bleed": -1}])
def test_bleed_rejections(tool_ws, doc, patch) -> None:
    op = {"op": "resize_canvas", "width": "108", "height": "108", **patch}
    _rejected(tool_ws, doc, op, match=r"ops\[0\] resize_canvas")


# --- normalize_viewbox ----------------------------------------------------------------


def test_normalize_viewbox_synthesizes_a_missing_viewbox(tool_ws) -> None:
    doc_id = open_doc(tool_ws, "nv.svg", NO_VIEWBOX)
    result = run_ops(doc_id, {"op": "normalize_viewbox"})
    assert result.changed is True and "synthesized" in result.summary
    assert _svg(tool_ws, doc_id).get("viewBox") == "0 0 80 40"


def test_normalize_viewbox_on_a_valid_viewbox_is_a_noop(tool_ws, doc) -> None:
    before, counts = _working(tool_ws, doc), _counts(tool_ws, doc)
    result = run_ops(doc, {"op": "normalize_viewbox"})
    assert "already normalized" in result.summary
    _assert_noop(tool_ws, doc, result, before, counts)


# --- fit_to_content --------------------------------------------------------------------


@pytest.fixture
def offset_doc(tool_ws: Path) -> str:
    return open_doc(tool_ws, "offset.svg", OFFSET)


def test_fit_to_content_measures_the_in_memory_tree(tool_ws, doc, monkeypatch) -> None:
    """The engine sees a materialized copy that already holds earlier ops of the same call."""
    seen: list[bytes] = []

    def fake_bbox(tree: Any, path: Path, settings: Any = None) -> tuple[float, float, float, float]:
        seen.append(path.read_bytes())
        return (40.0, 30.0, 20.0, 50.0)

    monkeypatch.setattr(transform_engine, "content_bbox_user_space", fake_bbox)
    before = _working(tool_ws, doc)
    result = run_ops(
        doc,
        {"op": "create_rect", "x": 1, "y": 1, "width": 2, "height": 2, "id": "fresh"},
        {"op": "fit_to_content"},
    )
    assert b'id="fresh"' in seen[0]
    assert _svg(tool_ws, doc).get("viewBox") == "40 30 20 50"
    restore_snapshot(doc, result.snapshot_id)
    assert _working(tool_ws, doc) == before


def test_fit_to_content_twice_is_a_noop_the_second_time(tool_ws, doc, monkeypatch) -> None:
    monkeypatch.setattr(
        transform_engine, "content_bbox_user_space", lambda *_a, **_k: (40.0, 30.0, 20.0, 50.0)
    )
    assert run_ops(doc, {"op": "fit_to_content"}).changed is True
    before, counts = _working(tool_ws, doc), _counts(tool_ws, doc)
    _assert_noop(tool_ws, doc, run_ops(doc, {"op": "fit_to_content"}), before, counts)


def test_fit_to_content_dry_run_only_describes_the_step(tool_ws, doc) -> None:
    before = _working(tool_ws, doc)
    plan = run_ops(doc, {"op": "fit_to_content"}, dry_run=True)
    assert plan.dry_run is True and plan.changed is False
    assert "would fit" in plan.outcomes[0].summary
    assert _working(tool_ws, doc) == before


@pytest.mark.inkscape
def test_fit_to_content_with_the_real_engine(tool_ws, offset_doc) -> None:
    before = _working(tool_ws, offset_doc)
    result = run_ops(offset_doc, {"op": "fit_to_content"})
    assert result.changed is True
    assert _svg(tool_ws, offset_doc).get("viewBox") == "40 30 20 50"
    after, counts = _working(tool_ws, offset_doc), _counts(tool_ws, offset_doc)
    # a second fit is a genuine no-op
    _assert_noop(tool_ws, offset_doc, run_ops(offset_doc, {"op": "fit_to_content"}), after, counts)
    restore_snapshot(offset_doc, result.snapshot_id)
    assert _working(tool_ws, offset_doc) == before


@pytest.mark.parametrize(
    "bbox", [[0, 0, -5, 5], [0, 0, float("nan"), 5], {"x": 0, "y": 0, "width": 5, "height": 5e999}]
)
def test_selector_bbox_rejects_negative_or_non_finite_sizes(tool_ws, doc, bbox) -> None:
    _rejected(tool_ws, doc, {"op": "move", "select": {"bbox": bbox}, "dx": 1})


def test_selector_bbox_accepts_the_list_form_inspect_reports(tool_ws, doc) -> None:
    result = run_ops(
        doc, {"op": "move", "select": {"tag": "rect", "bbox": [0, 0, 15, 15]}, "dx": 1}
    )
    assert result.outcomes[0].matched == ["r1"]


def test_repeated_moves_fold_into_one_translate(tool_ws, doc) -> None:
    for _ in range(3):
        run_ops(doc, {"op": "move", "ids": ["r1"], "dx": 5})
    run_ops(doc, {"op": "move", "ids": ["r1"], "dx": -1, "dy": 2})
    assert _transform(tool_ws, doc) == "translate(14,2)"
