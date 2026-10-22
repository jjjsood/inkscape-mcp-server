"""Style ops of `edit`: fill, stroke, opacity, replace_color, palette.

Covers the written values, honest `changed` (a re-applied value writes no snapshot and no record),
colour/paint validation (CSS injection, external urls), scoped replacement, error paths and undo.
Hermetic: the default edit renders nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from lxml import etree

from inkscape_mcp.edit.ops import OpError
from inkscape_mcp.edit.pipeline import EditApplyError
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.workspace import sandbox
from tests.conftest import run_ops
from tests.tool_support import open_doc

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
    b'<rect id="r1" fill="#ff0000"/>'
    b'<rect id="r2" style="fill:#00ff00"/>'
    b"</svg>"
)


@pytest.fixture
def doc(tool_ws: Path) -> str:
    return open_doc(tool_ws, "logo.svg", SVG)


def _working(root: Path, doc_id: str) -> bytes:
    return sandbox.working_copy(root, doc_id).read_bytes()


def _elem(root: Path, doc_id: str, oid: str) -> etree._Element:
    tree = etree.fromstring(_working(root, doc_id))
    return next(e for e in tree.iter() if isinstance(e.tag, str) and e.get("id") == oid)


def _props(elem: etree._Element) -> dict[str, str]:
    decls: dict[str, str] = {}
    for part in (elem.get("style") or "").split(";"):
        if ":" in part:
            key, value = part.split(":", 1)
            decls[key.strip()] = value.strip()
    return decls


def _paint(elem: etree._Element, prop: str = "fill") -> str | None:
    """The effective inline value: style block first, else the presentation attribute."""
    return _props(elem).get(prop) or elem.get(prop)


def _count(root: Path, doc_id: str) -> tuple[int, int]:
    """(snapshots, non-discarded operation records) written so far."""
    snaps = sandbox.snapshots_dir(root, doc_id)
    ops = sandbox.operations_dir(root, doc_id)
    records = [json.loads(p.read_text()) for p in ops.glob("op_*.json")] if ops.exists() else []
    return (
        len(list(snaps.glob("*.svg"))) if snaps.exists() else 0,
        sum(r["status"] != "discarded" for r in records),
    )


def _rejected(root: Path, doc_id: str, *ops: dict[str, Any], match: str | None = None) -> None:
    before, counts = _working(root, doc_id), _count(root, doc_id)
    with pytest.raises(OpError, match=match):
        run_ops(doc_id, *ops)
    assert _working(root, doc_id) == before
    assert _count(root, doc_id) == counts


def _fill(ids: list[str], color: str, **kw: Any) -> dict[str, Any]:
    return {"op": "fill", "ids": ids, "color": color, **kw}


# --- fill ---------------------------------------------------------------------


def test_fill_writes_style_and_records_one_medium_operation(tool_ws, doc) -> None:
    result = run_ops(doc, _fill(["r1"], "#0000ff"))
    assert _props(_elem(tool_ws, doc, "r1"))["fill"] == "#0000ff"
    assert result.changed is True and result.snapshot_id.startswith("snap_")
    assert _count(tool_ws, doc) == (1, 1)
    record = json.loads(
        (sandbox.operations_dir(tool_ws, doc) / f"{result.operation_id}.json").read_text()
    )
    assert (record["tool"], record["status"], record["risk_class"]) == ("edit", "applied", "medium")
    assert record["snapshot_id"] == result.snapshot_id
    # the original source file is never touched
    assert (tool_ws / "logo.svg").read_bytes() == SVG


def test_fill_opacity_applies_to_every_target(tool_ws, doc) -> None:
    run_ops(doc, _fill(["r1", "r2"], "blue", opacity=0.5))
    for oid in ("r1", "r2"):
        props = _props(_elem(tool_ws, doc, oid))
        assert (props["fill"], props["fill-opacity"]) == ("blue", "0.5")


def test_fill_is_undone_by_its_snapshot(tool_ws, doc) -> None:
    before = _working(tool_ws, doc)
    result = run_ops(doc, _fill(["r1"], "#abcdef"))
    assert _working(tool_ws, doc) != before
    restore_snapshot(doc, result.snapshot_id)
    assert _working(tool_ws, doc) == before


@pytest.mark.parametrize(
    ("oid", "color"),
    [
        ("r1", "#ff0000"),  # same value as the presentation attribute
        ("r1", "#F00"),  # same colour under case / 3-digit shorthand
        ("r2", "#00ff00"),  # same value as the inline style
    ],
)
def test_reapplying_the_same_colour_is_a_true_noop(tool_ws, doc, oid, color) -> None:
    before, counts = _working(tool_ws, doc), _count(tool_ws, doc)
    result = run_ops(doc, _fill([oid], color))
    assert result.changed is False
    assert result.operation_id == "" and result.snapshot_id == ""
    # byte-identical: an attribute is NOT migrated into a style block
    assert _working(tool_ws, doc) == before
    assert _count(tool_ws, doc) == counts


@pytest.mark.parametrize(
    "paint",
    [
        "url(#grad1)",
        "url(#MyGrad) #ff0000",  # id kept verbatim (case-sensitive) with its fallback colour
    ],
)
def test_fill_accepts_paint_server_references(tool_ws, doc, paint) -> None:
    assert run_ops(doc, _fill(["r1"], paint)).changed is True
    assert _props(_elem(tool_ws, doc, "r1"))["fill"] == paint


def test_stroke_accepts_a_paint_server_reference(tool_ws, doc) -> None:
    run_ops(doc, {"op": "stroke", "ids": ["r1"], "color": "url(#grad1)"})
    assert _props(_elem(tool_ws, doc, "r1"))["stroke"] == "url(#grad1)"


@pytest.mark.parametrize(
    "bad",
    [
        "red;stroke:url(#x)",
        "blue}",
        "#fff{color:red",
        "rgb(0,0,0);evil:1",
        "url(http://evil.example/x)",  # external url is never written
        "url(#id);fill:red",
        "url(#id) javascript:alert(1)",
        "javascript:alert(1)",
        "url(#bad id)",
    ],
)
def test_unsafe_colors_are_rejected_without_a_write(tool_ws, doc, bad) -> None:
    _rejected(tool_ws, doc, _fill(["r1"], bad), match=r"ops\[0\] fill")


# --- stroke / opacity -----------------------------------------------------------


def test_stroke_sets_exactly_the_given_properties(tool_ws, doc) -> None:
    run_ops(
        doc, {"op": "stroke", "ids": ["r1"], "color": "#333333", "width": "2px", "opacity": 0.25}
    )
    props = _props(_elem(tool_ws, doc, "r1"))
    assert (props["stroke"], props["stroke-width"], props["stroke-opacity"]) == (
        "#333333",
        "2px",
        "0.25",
    )


def test_stroke_needs_at_least_one_property(tool_ws, doc) -> None:
    _rejected(tool_ws, doc, {"op": "stroke", "ids": ["r1"]}, match=r"ops\[0\] stroke")


def test_opacity_is_written_and_range_checked(tool_ws, doc) -> None:
    run_ops(doc, {"op": "opacity", "ids": ["r1"], "opacity": 0.4})
    assert _props(_elem(tool_ws, doc, "r1"))["opacity"] == "0.4"
    _rejected(tool_ws, doc, {"op": "opacity", "ids": ["r1"], "opacity": 1.5}, match="opacity")


# --- replace_color / palette ----------------------------------------------------


def test_replace_color_matches_attribute_shorthand_and_inline_style(tool_ws, doc) -> None:
    result = run_ops(doc, {"op": "replace_color", "from_color": "#f00", "to_color": "#0000ff"})
    assert _paint(_elem(tool_ws, doc, "r1")) == "#0000ff"
    assert "1 object" in result.summary
    result = run_ops(doc, {"op": "replace_color", "from_color": "#00FF00", "to_color": "#000000"})
    assert _props(_elem(tool_ws, doc, "r2"))["fill"] == "#000000"
    assert "1 object" in result.summary


def test_replace_color_with_zero_matches_is_a_noop(tool_ws, doc) -> None:
    before, counts = _working(tool_ws, doc), _count(tool_ws, doc)
    result = run_ops(doc, {"op": "replace_color", "from_color": "#abcdef", "to_color": "#123123"})
    assert result.changed is False
    assert result.operation_id == "" and result.snapshot_id == ""
    assert "0 object" in result.summary
    assert _working(tool_ws, doc) == before and _count(tool_ws, doc) == counts


def test_replace_color_scope_limits_the_change(tool_ws, doc) -> None:
    # r1 and r2 both turn blue document-wide, but only r2 is in scope here
    run_ops(doc, {"op": "palette", "mapping": {"#ff0000": "#00ff00"}})
    run_ops(
        doc,
        {"op": "replace_color", "from_color": "#00ff00", "to_color": "#123456", "ids": ["r2"]},
    )
    assert _paint(_elem(tool_ws, doc, "r2")) == "#123456"
    assert _paint(_elem(tool_ws, doc, "r1")) == "#00ff00"


def test_palette_applies_every_mapping(tool_ws, doc) -> None:
    result = run_ops(
        doc, {"op": "palette", "mapping": {"#ff0000": "#111111", "#00ff00": "#222222"}}
    )
    assert _paint(_elem(tool_ws, doc, "r1")) == "#111111"
    assert _paint(_elem(tool_ws, doc, "r2")) == "#222222"
    assert "2 mappings" in result.summary


def test_palette_scope_via_selector(tool_ws, doc) -> None:
    run_ops(
        doc, {"op": "palette", "mapping": {"#ff0000": "#111111"}, "select": {"id_prefix": "r2"}}
    )
    assert _paint(_elem(tool_ws, doc, "r1")) == "#ff0000"


def test_palette_with_no_matches_is_a_noop(tool_ws, doc) -> None:
    assert run_ops(doc, {"op": "palette", "mapping": {"#aaaaaa": "#bbbbbb"}}).changed is False


def test_palette_accepts_a_named_colour(tool_ws, doc) -> None:
    assert run_ops(doc, {"op": "palette", "mapping": {"#ff0000": "blue"}}).changed is True
    assert _paint(_elem(tool_ws, doc, "r1")) == "blue"


@pytest.mark.parametrize(
    "mapping",
    [
        {"#3366cc": "notacolor"},  # letters-only but not a CSS colour keyword
        {"notacolor": "#000000"},
    ],
)
def test_palette_validates_colours_before_any_record(tool_ws, doc, mapping) -> None:
    _rejected(tool_ws, doc, {"op": "palette", "mapping": mapping}, match=r"ops\[0\] palette")


# --- targets and shared error paths ---------------------------------------------


def test_one_call_restyles_with_one_snapshot(tool_ws, doc) -> None:
    run_ops(doc, _fill(["r1"], "#0000ff"), {"op": "stroke", "ids": ["r1"], "color": "#000"})
    assert _count(tool_ws, doc) == (1, 1)


def test_missing_and_empty_targets_are_rejected(tool_ws, doc) -> None:
    _rejected(
        tool_ws, doc, _fill(["does-not-exist"], "#000000"), match="id 'does-not-exist' not found"
    )
    _rejected(tool_ws, doc, _fill([], "#000000"), match="'ids' is empty")
    _rejected(
        tool_ws, doc, _fill(["r1"], "#000000"), _fill(["r9"], "#000000"), match=r"ops\[1\] fill"
    )


def test_unknown_document(doc) -> None:
    with pytest.raises(EditApplyError, match="document id not found"):
        run_ops("d_nope", _fill(["r1"], "#000000"))
