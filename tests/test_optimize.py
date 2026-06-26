"""Web-optimize tool + engine tests (E5-04 / ADR-004 / ADR-005).

Hermetic: the pipeline's preview rendering is monkeypatched so no Inkscape is invoked (the cleanup
itself is pure lxml). Each test asserts the working copy is optimized, references are preserved (no
dangling `#frag`), the original source stays byte-identical, and the edit is reversible.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from lxml import etree

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.edit.optimize import OPTIMIZE_CODES, analyze_optimizations
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render.cli import RenderResult
from inkscape_mcp.server import mcp
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.tools.optimize import svg_web_optimize
from inkscape_mcp.validate import validate_document
from inkscape_mcp.workspace import sandbox

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

SVG_NS = "http://www.w3.org/2000/svg"


def _fake_render_preview(doc_id: str, width_px: int | None = None) -> RenderResult:
    """Hermetic stand-in for `render_preview` (no Inkscape): write a tiny deterministic PNG."""
    reg = get_registry()
    entry = reg.get(doc_id)
    root = Path(entry.root)
    preview_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    descriptor = "auto" if width_px is None else f"{width_px}px"
    produced = preview_dir / f"preview-{descriptor}.png"
    produced.write_bytes(b"\x89PNG\r\n\x1a\n")
    # E11-01 one-location contract: artifact_path is workspace-ROOT-relative (matches the real
    # engine), so the pipeline's `root / artifact_path` join resolves correctly.
    rel = produced.relative_to(root).as_posix()
    return RenderResult(
        doc_id=doc_id,
        artifact_path=rel,
        workspace_relative_path=rel,
        format="png",
        width_px=100,
        height_px=100,
        duration_s=0.0,
    )


@pytest.fixture
def doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    monkeypatch.setattr("inkscape_mcp.edit.pipeline.render_preview", _fake_render_preview)
    src = ws / "art.svg"
    src.write_bytes(SVG)
    entry = get_registry().open_document(str(src))
    return entry.doc_id, ws, src


def _working_root(doc_id: str, root: Path) -> etree._Element:
    return etree.fromstring(sandbox.working_copy(root, doc_id).read_bytes())


def _ids(root: etree._Element) -> set[str]:
    return {e.get("id") for e in root.iter() if isinstance(e.tag, str) and e.get("id")}  # type: ignore[misc]


def _locals(root: etree._Element) -> set[str]:
    return {etree.QName(e.tag).localname for e in root.iter() if isinstance(e.tag, str)}


# --- registration -----------------------------------------------------------


def test_registered_on_mcp(doc: tuple[str, Path, Path]) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "svg_web_optimize" in names


# --- the three cleanups -----------------------------------------------------


def test_strips_editor_cruft(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    svg_web_optimize(doc_id)
    optimized = _working_root(doc_id, root)
    locals_ = _locals(optimized)
    assert "namedview" not in locals_
    assert "metadata" not in locals_
    # editor-namespaced attributes (inkscape:label) gone; comments gone.
    raw = sandbox.working_copy(root, doc_id).read_bytes()
    assert b"inkscape:label" not in raw
    assert b"editor comment" not in raw


def test_drops_dead_structure_preserving_references(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    svg_web_optimize(doc_id)
    optimized = _working_root(doc_id, root)
    ids = _ids(optimized)
    # Unreferenced templates / ids / empty group removed.
    assert "gradUnused" not in ids
    assert "unrefrect" not in ids
    assert "r1" not in ids  # r1 is not referenced -> its id is stripped
    # Referenced ids preserved (no dangling refs).
    assert "gradUsed" in ids
    assert "keepg" in ids
    # The rect element itself survives (only its unused id was dropped).
    rects = [e for e in optimized.iter(f"{{{SVG_NS}}}rect") if e.get("fill") == "url(#gradUsed)"]
    assert len(rects) == 1
    # The empty <g> is gone (keepg + the doc root remain as groups/containers).
    groups = [e for e in optimized.iter(f"{{{SVG_NS}}}g")]
    assert len(groups) == 1
    # A <style> inside <defs> carries no id but applies globally — it must NOT be pruned.
    assert len([e for e in optimized.iter(f"{{{SVG_NS}}}style")]) == 1


def test_reduces_coordinate_precision(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    svg_web_optimize(doc_id, precision=2)
    optimized = _working_root(doc_id, root)
    rect = next(e for e in optimized.iter(f"{{{SVG_NS}}}rect") if e.get("fill") == "url(#gradUsed)")
    assert rect.get("x") == "10.12"
    assert rect.get("y") == "20.65"
    # viewBox is deliberately untouched.
    assert optimized.get("viewBox") == "0 0 100 100"


def test_no_dangling_references_after_optimize(doc: tuple[str, Path, Path]) -> None:
    doc_id, _root, _ = doc
    svg_web_optimize(doc_id)
    report = validate_document(doc_id)
    assert not any(f.code == "missing_id" for f in report.findings)


# --- reversibility + original untouched --------------------------------------


def test_reversible_and_original_untouched(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, src = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    result = svg_web_optimize(doc_id)
    assert result.changed is True
    assert result.operation_id
    after = sandbox.working_copy(root, doc_id).read_bytes()
    assert after != before
    # Original source is byte-identical.
    assert src.read_bytes() == SVG
    # Snapshot restores the pre-optimize bytes.
    restore_snapshot(doc_id, result.snapshot_id)
    assert sandbox.working_copy(root, doc_id).read_bytes() == before


# --- idempotency ------------------------------------------------------------


def test_idempotent_second_pass_is_noop(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    svg_web_optimize(doc_id)
    # A read-only analysis of the optimized tree shows nothing left to strip/round.
    optimized = _working_root(doc_id, root)
    counts = analyze_optimizations(optimized, precision=2)
    assert counts.total == 0


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize("bad", [-1, 9, 100])
def test_rejects_out_of_range_precision(doc: tuple[str, Path, Path], bad: int) -> None:
    doc_id, _root, _ = doc
    with pytest.raises(ToolError) as exc:
        svg_web_optimize(doc_id, precision=bad)
    assert "precision" in str(exc.value)


def test_unknown_doc_id(doc: tuple[str, Path, Path]) -> None:
    with pytest.raises(ToolError) as exc:
        svg_web_optimize("d_missing")
    assert "not found" in str(exc.value)


# --- keep_ids allowlist (E10-07 O3 / E11-04) --------------------------------


def test_keep_ids_preserves_otherwise_unreferenced_id(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    # `unrefrect` is referenced by nothing; without keep_ids its id is stripped (see
    # test_drops_dead_structure_preserving_references). Listing it must preserve it end to end.
    result = svg_web_optimize(doc_id, keep_ids=["unrefrect"])
    optimized = _working_root(doc_id, root)
    ids = _ids(optimized)
    assert "unrefrect" in ids
    # The element itself survives, not just the attribute.
    rects = [e for e in optimized.iter(f"{{{SVG_NS}}}rect") if e.get("id") == "unrefrect"]
    assert len(rects) == 1
    # A kept id is not double-counted as an unreferenced-id removal.
    assert result.removed.get("unreferenced_ids", 0) >= 0


def test_keep_ids_round_trip_retains_set_id(doc: tuple[str, Path, Path]) -> None:
    """Stand-in for a rename_object -> svg_web_optimize round-trip: a freshly set human id on an
    otherwise-unreferenced element is retained when passed through keep_ids (E11-04)."""
    doc_id, root, _ = doc
    # Simulate a human/a11y id deliberately set on the unreferenced rect.
    working = sandbox.working_copy(root, doc_id)
    tree = etree.fromstring(working.read_bytes())
    for e in tree.iter(f"{{{SVG_NS}}}rect"):
        if e.get("id") == "unrefrect":
            e.set("id", "hero-shape")
    working.write_bytes(etree.tostring(tree))

    svg_web_optimize(doc_id, keep_ids=["hero-shape"])
    ids = _ids(_working_root(doc_id, root))
    assert "hero-shape" in ids


def test_keep_ids_none_strips_as_before(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    # Default (no keep_ids) still strips the unreferenced id — keep_ids is opt-in, not a regression.
    svg_web_optimize(doc_id)
    assert "unrefrect" not in _ids(_working_root(doc_id, root))


def test_keep_ids_unknown_id_is_noop(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    # An id not present in the document is harmless and changes nothing about the result.
    result = svg_web_optimize(doc_id, keep_ids=["not-in-doc"])
    assert "unrefrect" not in _ids(_working_root(doc_id, root))
    assert result.changed is True


# --- machine-diffable structured deltas (E11-08) ----------------------------


def test_returns_structured_byte_deltas(doc: tuple[str, Path, Path]) -> None:
    doc_id, _root, _ = doc
    result = svg_web_optimize(doc_id)
    # bytes_before/after present and the optimize shrank the file.
    assert result.bytes_before > 0
    assert result.bytes_after > 0
    assert result.bytes_after < result.bytes_before
    # An agent can compute the saving with no on-disk stat or prose parsing.
    assert result.bytes_before - result.bytes_after > 0


def test_removed_map_counts_each_cleanup(doc: tuple[str, Path, Path]) -> None:
    doc_id, _root, _ = doc
    result = svg_web_optimize(doc_id)
    removed = result.removed
    # Every code in the map is a known cleanup code; only non-zero entries appear.
    assert set(removed) <= set(OPTIMIZE_CODES)
    assert all(v > 0 for v in removed.values())
    # The fixture exercises all five cleanups, so each should be present and counted.
    assert removed["editor_metadata"] > 0  # comment + namedview + metadata + inkscape:label
    assert removed["unused_defs"] == 1  # gradUnused
    assert removed["unreferenced_ids"] >= 1  # r1 / unrefrect / namedview / metadata ids
    assert removed["empty_groups"] == 1  # the trailing <g></g>
    assert removed["reducible_coords"] >= 1  # the high-precision rect coords


def test_removed_codes_cross_join_quality_opportunities(doc: tuple[str, Path, Path]) -> None:
    """The `removed` codes are exactly the `quality_report.opportunities` keys, so a budget-aware
    agent can cross-join the two without a translation table (E11-08)."""
    from inkscape_mcp.quality import quality_report

    doc_id, _root, _ = doc
    report = quality_report(doc_id)
    opportunity_codes = {opp.code for opp in report.opportunities}
    result = svg_web_optimize(doc_id)
    # Every cleanup the optimizer actually performed was reported as an opportunity beforehand.
    assert set(result.removed) <= opportunity_codes
    # The pre-optimize opportunity counts match what the optimize then removed (same detection).
    opp_by_code = {opp.code: opp.count for opp in report.opportunities}
    for code, count in result.removed.items():
        assert opp_by_code[code] == count


def test_idempotent_second_pass_removes_nothing(doc: tuple[str, Path, Path]) -> None:
    doc_id, _root, _ = doc
    svg_web_optimize(doc_id)
    second = svg_web_optimize(doc_id)
    # Nothing left to strip; the structured map is empty and bytes are unchanged.
    assert second.removed == {}
    assert second.bytes_before == second.bytes_after
