"""`optimize_web` op: strip editor data, drop dead structure, round coordinates (pure lxml).

Each case asserts the working copy is optimized, references survive (no dangling `#frag`), the
original source stays byte-identical and the edit is one recorded, reversible snapshot. The
machine-diffable deltas (`bytes_before/after`, `removed`) are an engine feature (`deltas_holder`),
no longer part of the `edit` result, so they are asserted on the engine directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from inkscape_mcp.edit.ops import OpError
from inkscape_mcp.edit.optimize import (
    OPTIMIZE_CODES,
    WebOptimizeDeltas,
    analyze_optimizations,
    optimize_web_mutate,
)
from inkscape_mcp.edit.pipeline import EditApplyError
from inkscape_mcp.operations import OperationStatus, get_operation
from inkscape_mcp.quality import quality_report
from inkscape_mcp.snapshots import list_snapshots, restore_snapshot
from inkscape_mcp.validate import validate_document
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.xml_safety import parse_svg_bytes
from tests.conftest import run_ops
from tests.tool_support import call, open_doc

SVG_NS = "http://www.w3.org/2000/svg"

#: every kind of cruft the optimizer strips, a `<style>` and a `<script>` in `<defs>` (global scope,
#: no id: never pruned) and a referenced group / gradient so reference preservation can be checked
SVG = (
    b'<?xml version="1.0"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg"'
    b' xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"'
    b' xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.0.dtd"'
    b' xmlns:xlink="http://www.w3.org/1999/xlink"'
    b' width="100" height="100" viewBox="0 0 100 100">'
    b"<!-- an editor comment -->"
    b'<sodipodi:namedview id="namedview1" pagecolor="#ffffff"/>'
    b'<metadata id="metadata1">rdf junk</metadata>'
    b"<defs>"
    b"<style>.foo{fill:#000000}</style>"
    b'<script type="text/javascript">/* keep me */</script>'
    b'<linearGradient id="gradUsed"><stop offset="0.123456" stop-color="#ff0000"/></linearGradient>'
    b'<linearGradient id="gradUnused"><stop offset="1" stop-color="#00ff00"/></linearGradient>'
    b"</defs>"
    b'<g id="keepg" inkscape:label="Layer 1">'
    b'<rect id="r1" x="10.123456" y="20.654321" width="30.5" height="40.5" fill="url(#gradUsed)"/>'
    b"</g>"
    b'<use xlink:href="#keepg" x="0" y="0"/>'
    b'<rect id="unrefrect" x="5.111111" y="5.222222" width="2" height="2" fill="#0000ff"/>'
    b"<g></g>"
    b"</svg>"
)
OPTIMIZE = {"op": "optimize_web"}


@pytest.fixture
def doc(tool_ws: Path) -> str:
    return open_doc(tool_ws, "art.svg", SVG)


def _working(ws: Path, doc_id: str) -> etree._Element:
    return etree.fromstring(sandbox.working_copy(ws, doc_id).read_bytes())


def _ids(root: etree._Element) -> set[str]:
    return {e.get("id") for e in root.iter() if isinstance(e.tag, str) and e.get("id")}


def _locals(root: etree._Element) -> set[str]:
    return {etree.QName(e.tag).localname for e in root.iter() if isinstance(e.tag, str)}


def _optimized_ids(ws: Path, doc_id: str, **op: object) -> set[str]:
    run_ops(doc_id, {**OPTIMIZE, **op})
    return _ids(_working(ws, doc_id))


# --- the three cleanups ---------------------------------------------------------


def test_strips_editor_cruft(doc: str, tool_ws: Path) -> None:
    run_ops(doc, OPTIMIZE)
    assert not {"namedview", "metadata"} & _locals(_working(tool_ws, doc))
    raw = sandbox.working_copy(tool_ws, doc).read_bytes()
    assert b"inkscape:label" not in raw and b"editor comment" not in raw


def test_drops_dead_structure_but_preserves_references(doc: str, tool_ws: Path) -> None:
    run_ops(doc, OPTIMIZE)
    optimized = _working(tool_ws, doc)
    ids = _ids(optimized)
    assert not {"gradUnused", "unrefrect", "r1"} & ids  # unreferenced: dropped / id stripped
    assert {"gradUsed", "keepg"} <= ids  # referenced: kept
    assert len([e for e in optimized.iter(f"{{{SVG_NS}}}rect") if e.get("fill")]) == 2
    assert len(list(optimized.iter(f"{{{SVG_NS}}}g"))) == 1  # the empty <g> is gone


def test_global_style_and_script_in_defs_are_never_pruned(doc: str, tool_ws: Path) -> None:
    run_ops(doc, OPTIMIZE)
    optimized = _working(tool_ws, doc)
    assert len(list(optimized.iter(f"{{{SVG_NS}}}style"))) == 1
    assert len(list(optimized.iter(f"{{{SVG_NS}}}script"))) == 1
    assert not {"namedview", "metadata"} & _locals(optimized)  # cruft went on the same pass


def test_reduces_coordinate_precision_but_not_the_viewbox(doc: str, tool_ws: Path) -> None:
    run_ops(doc, {**OPTIMIZE, "precision": 2})
    optimized = _working(tool_ws, doc)
    rect = next(e for e in optimized.iter(f"{{{SVG_NS}}}rect") if e.get("fill") == "url(#gradUsed)")
    assert (rect.get("x"), rect.get("y")) == ("10.12", "20.65")
    assert optimized.get("viewBox") == "0 0 100 100"


def test_leaves_no_dangling_reference(doc: str) -> None:
    run_ops(doc, OPTIMIZE)
    assert not any(f.code == "missing_id" for f in validate_document(doc).findings)


# --- reversible, recorded, idempotent ----------------------------------------------


def test_one_recorded_reversible_snapshot_and_the_original_is_untouched(
    doc: str, tool_ws: Path
) -> None:
    before = sandbox.working_copy(tool_ws, doc).read_bytes()
    res = run_ops(doc, OPTIMIZE)
    assert res.changed and res.risk == "medium" and not res.needs_approval
    assert res.operation_id.startswith("op_") and res.snapshot_id.startswith("snap_")
    assert sandbox.working_copy(tool_ws, doc).read_bytes() != before

    assert [s.snapshot_id for s in list_snapshots(doc)] == [res.snapshot_id]
    record = get_operation(doc, res.operation_id)
    assert record.tool == "edit" and record.status is OperationStatus.APPLIED
    assert record.snapshot_id == res.snapshot_id

    restore_snapshot(doc, res.snapshot_id)
    assert sandbox.working_copy(tool_ws, doc).read_bytes() == before
    assert (tool_ws / "art.svg").read_bytes() == SVG


def test_restoring_through_the_history_tool(tool_app, tool_ws: Path, doc: str) -> None:
    before = sandbox.working_copy(tool_ws, doc).read_bytes()
    done = call(tool_app, "edit", {"doc_id": doc, "ops": [{**OPTIMIZE, "precision": 2}]}).data
    assert done["changed"] is True and done["snapshot_id"].startswith("snap_")
    call(
        tool_app,
        "history",
        {"doc_id": doc, "action": "restore", "snapshot_id": done["snapshot_id"]},
    )
    assert sandbox.working_copy(tool_ws, doc).read_bytes() == before


def test_dry_run_plans_and_writes_nothing(tool_app, tool_ws: Path, doc: str) -> None:
    before = sandbox.working_copy(tool_ws, doc).read_bytes()
    plan = call(tool_app, "edit", {"doc_id": doc, "ops": [OPTIMIZE], "dry_run": True}).data
    assert plan["dry_run"] is True and plan["changed"] is False
    assert not plan.get("needs_approval")
    assert sandbox.working_copy(tool_ws, doc).read_bytes() == before
    assert list_snapshots(doc) == []


def test_second_pass_is_a_noop(doc: str, tool_ws: Path) -> None:
    run_ops(doc, OPTIMIZE)
    assert analyze_optimizations(_working(tool_ws, doc), precision=2).total == 0
    again = run_ops(doc, OPTIMIZE)
    assert again.changed is False and again.snapshot_id == ""
    assert len(list_snapshots(doc)) == 1


# --- validation ----------------------------------------------------------------------


@pytest.mark.parametrize("bad", [-1, 9, 100])
def test_out_of_range_precision_is_refused(doc: str, tool_ws: Path, bad: int) -> None:
    before = sandbox.working_copy(tool_ws, doc).read_bytes()
    with pytest.raises(OpError, match=r"ops\[0\] optimize_web: precision must be between 0 and 8"):
        run_ops(doc, {**OPTIMIZE, "precision": bad})
    assert sandbox.working_copy(tool_ws, doc).read_bytes() == before


def test_unknown_document(tool_ws: Path) -> None:
    with pytest.raises(EditApplyError, match="document id not found"):
        run_ops("d_missing", OPTIMIZE)


# --- keep_ids allowlist -----------------------------------------------------------------


def test_keep_ids_preserves_an_otherwise_unreferenced_element(doc: str, tool_ws: Path) -> None:
    ids = _optimized_ids(tool_ws, doc, keep_ids=["unrefrect"])
    assert "unrefrect" in ids
    assert len([e for e in _working(tool_ws, doc).iter(f"{{{SVG_NS}}}rect") if e.get("id")]) == 1


def test_keep_ids_keeps_a_deliberately_renamed_id(doc: str, tool_ws: Path) -> None:
    working = sandbox.working_copy(tool_ws, doc)
    tree = etree.fromstring(working.read_bytes())
    next(e for e in tree.iter(f"{{{SVG_NS}}}rect") if e.get("id") == "unrefrect").set(
        "id", "hero-shape"
    )
    working.write_bytes(etree.tostring(tree))
    assert "hero-shape" in _optimized_ids(tool_ws, doc, keep_ids=["hero-shape"])


def test_without_keep_ids_the_unreferenced_id_is_stripped(doc: str, tool_ws: Path) -> None:
    assert "unrefrect" not in _optimized_ids(tool_ws, doc)


def test_an_unknown_keep_id_is_harmless(doc: str, tool_ws: Path) -> None:
    assert "unrefrect" not in _optimized_ids(tool_ws, doc, keep_ids=["not-in-doc"])


# --- machine-diffable deltas (engine level) ---------------------------------------------


def _run_engine(**kw: object) -> tuple[WebOptimizeDeltas, etree._ElementTree]:
    tree = parse_svg_bytes(SVG)
    holder: list[WebOptimizeDeltas] = []
    optimize_web_mutate(deltas_holder=holder, **kw)(tree)  # type: ignore[arg-type]
    return holder[0], tree


def test_deltas_report_the_byte_saving() -> None:
    deltas, _ = _run_engine()
    assert 0 < deltas.bytes_after < deltas.bytes_before
    assert deltas.bytes_saved == deltas.bytes_before - deltas.bytes_after


def test_removed_map_counts_each_of_the_five_cleanups() -> None:
    removed = _run_engine()[0].removed
    assert set(removed) <= set(OPTIMIZE_CODES) and all(v > 0 for v in removed.values())
    assert removed["editor_metadata"] > 0  # comment + namedview + metadata + inkscape:label
    assert removed["unused_defs"] == 1  # gradUnused
    assert removed["unreferenced_ids"] >= 1
    assert removed["empty_groups"] == 1  # the trailing <g></g>
    assert removed["reducible_coords"] >= 1


def test_removed_codes_cross_join_the_quality_report(doc: str) -> None:
    opportunities = {o.code: o.count for o in quality_report(doc).opportunities}
    removed = _run_engine()[0].removed
    assert set(removed) <= set(opportunities)
    assert all(opportunities[code] == count for code, count in removed.items())


def test_a_second_engine_pass_removes_nothing() -> None:
    _, tree = _run_engine()
    holder: list[WebOptimizeDeltas] = []
    optimize_web_mutate(deltas_holder=holder)(tree)
    assert holder[0].removed == {} and holder[0].bytes_before == holder[0].bytes_after
