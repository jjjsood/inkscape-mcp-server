"""Transform tool + engine tests (ADR-002 / ADR-004 / ADR-005).

Hermetic: `render_preview` is monkeypatched in the pipeline module so no test invokes Inkscape.
The fake writes a tiny deterministic PNG into `artifacts/preview/preview-auto.png` and returns a
`RenderResult` (the real engine's no-width shape), which the pipeline then copies to the
operation-specific before/after names.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from lxml import etree

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.edit import pipeline
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render.cli import RenderResult
from inkscape_mcp.server import mcp
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.tools.transform import (
    fit_to_content,
    move_object,
    normalize_viewbox,
    resize_canvas,
    rotate_object,
    scale_object,
    tile,
)
from inkscape_mcp.workspace import sandbox

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" '
    b'viewBox="0 0 100 100"><rect id="r1" x="0" y="0" width="10" height="10"/></svg>'
)

#: A document whose content does NOT fill the canvas: a 200x200 page with a single rect at
#: (40,30) sized 20x50, so the content bbox is "40 30 20 50" (verified against Inkscape
#: --query-all). `fit_to_content` should retarget the viewBox to exactly that box.
SVG_OFFSET_CONTENT = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" '
    b'viewBox="0 0 200 200"><rect id="r1" x="40" y="30" width="20" height="50"/></svg>'
)

#: SVG with no viewBox (synthesis source) but numeric width/height.
SVG_NO_VIEWBOX = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40">'
    b'<rect id="r1" x="0" y="0" width="10" height="10"/></svg>'
)

SVG_NS = "http://www.w3.org/2000/svg"

#: A minimal PNG signature so the written file is plausibly a raster (content is irrelevant here).
PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-preview"


def _make_doc(ws: Path, data: bytes) -> tuple[str, Path, Path]:
    """Open a fixture SVG with the given bytes; return (doc_id, owning_root, source_path)."""
    reset_registry()
    src = ws / "logo.svg"
    src.write_bytes(data)
    entry = get_registry().open_document(str(src))
    return entry.doc_id, ws, src


@pytest.fixture
def doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path, Path]:
    """Open the viewBox fixture; return (doc_id, owning_root, original_source_path)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    return _make_doc(ws, SVG)


@pytest.fixture
def doc_no_viewbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path, Path]:
    """Open the no-viewBox fixture; return (doc_id, owning_root, original_source_path)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    return _make_doc(ws, SVG_NO_VIEWBOX)


@pytest.fixture
def doc_offset_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path, Path]:
    """Open the offset-content fixture; return (doc_id, owning_root, original_source_path)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    return _make_doc(ws, SVG_OFFSET_CONTENT)


@pytest.fixture(autouse=True)
def fake_render(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace `render_preview` in the pipeline with a hermetic fake (no Inkscape)."""

    def fake_render_preview(
        doc_id: str, width_px: int | None = None, settings: object | None = None
    ) -> RenderResult:
        entry = get_registry().get(doc_id)
        root = Path(entry.root)
        preview_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        out = preview_dir / "preview-auto.png"
        out.write_bytes(PNG_BYTES)
        # one-location contract: artifact_path is workspace-ROOT-relative (matches the real
        # engine), so the pipeline's `root / artifact_path` join resolves correctly.
        rel = out.relative_to(root).as_posix()
        return RenderResult(
            doc_id=doc_id,
            artifact_path=rel,
            workspace_relative_path=rel,
            format="png",
            width_px=100,
            height_px=100,
            duration_s=0.01,
        )

    monkeypatch.setattr(pipeline, "render_preview", fake_render_preview)


def _working_tree(root: Path, doc_id: str) -> etree._Element:
    """Re-parse the on-disk working copy and return its root element."""
    return etree.parse(str(sandbox.working_copy(root, doc_id))).getroot()


def _operation_record(root: Path, doc_id: str, operation_id: str) -> dict:
    op_file = sandbox.operations_dir(root, doc_id) / f"{operation_id}.json"
    return json.loads(op_file.read_text())


def _op_count(root: Path, doc_id: str) -> int:
    """Number of Operation Record files written for the document."""
    op_dir = sandbox.operations_dir(root, doc_id)
    return len(list(op_dir.glob("op_*.json"))) if op_dir.exists() else 0


def _snapshot_count(root: Path, doc_id: str) -> int:
    """Number of snapshot SVG files written for the document."""
    snap_dir = sandbox.snapshots_dir(root, doc_id)
    return len(list(snap_dir.glob("*.svg"))) if snap_dir.exists() else 0


# --- move_object ------------------------------------------------------------


def test_move_object_adds_translate_records_and_previews(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, src = doc
    original = src.read_bytes()

    result = move_object(doc_id, "r1", 5.0, -3.0)

    # The re-parsed working copy now carries a translate transform on r1.
    rect = _working_tree(root, doc_id).find(f".//{{{SVG_NS}}}rect")
    assert rect is not None
    assert rect.get("transform") == "translate(5,-3)"

    # Operation Record is applied/medium with both previews linked.
    assert result.changed is True
    record = _operation_record(root, doc_id, result.operation_id)
    assert record["status"] == "applied"
    assert record["risk_class"] == "medium"
    assert set(record["previews"]) == {"before", "after"}
    assert result.preview_before is not None
    assert result.preview_after is not None

    # The original source file is byte-unchanged.
    assert src.read_bytes() == original


# --- scale_object -----------------------------------------------------------


@pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
def test_scale_object_rejects_invalid_factor_no_change(
    doc: tuple[str, Path, Path], bad: float
) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()

    with pytest.raises(ToolError):
        scale_object(doc_id, "r1", bad)

    # Nothing changed on the working copy.
    assert working.read_bytes() == before


def test_scale_object_valid_factor_prepends_scale(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc

    scale_object(doc_id, "r1", 2.0)
    rect = _working_tree(root, doc_id).find(f".//{{{SVG_NS}}}rect")
    assert rect is not None
    assert rect.get("transform") == "scale(2,2)"


def test_scale_object_non_uniform(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    scale_object(doc_id, "r1", 2.0, 3.0)
    rect = _working_tree(root, doc_id).find(f".//{{{SVG_NS}}}rect")
    assert rect is not None
    assert rect.get("transform") == "scale(2,3)"


# --- rotate_object ----------------------------------------------------------


def test_rotate_object_about_centre(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    rotate_object(doc_id, "r1", 45.0, 50.0, 50.0)
    rect = _working_tree(root, doc_id).find(f".//{{{SVG_NS}}}rect")
    assert rect is not None
    assert rect.get("transform") == "rotate(45,50,50)"


def test_rotate_object_about_origin(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    rotate_object(doc_id, "r1", 90.0)
    rect = _working_tree(root, doc_id).find(f".//{{{SVG_NS}}}rect")
    assert rect is not None
    assert rect.get("transform") == "rotate(90)"


# --- resize_canvas ----------------------------------------------------------


def test_resize_canvas_changes_dimensions(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    resize_canvas(doc_id, "200px", "150px")
    svg_root = _working_tree(root, doc_id)
    assert svg_root.get("width") == "200px"
    assert svg_root.get("height") == "150px"
    # An existing valid viewBox is preserved.
    assert svg_root.get("viewBox") == "0 0 100 100"


def test_resize_canvas_rejects_injection_length(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()

    with pytest.raises(ToolError):
        resize_canvas(doc_id, "10; evil", "100")

    assert working.read_bytes() == before


# --- normalize_viewbox ------------------------------------------------------


def test_normalize_viewbox_synthesizes_when_absent(
    doc_no_viewbox: tuple[str, Path, Path],
) -> None:
    doc_id, root, _ = doc_no_viewbox
    result = normalize_viewbox(doc_id)
    svg_root = _working_tree(root, doc_id)
    assert svg_root.get("viewBox") == "0 0 80 40"
    assert "synthesized" in (result.summary or "")


def test_normalize_viewbox_idempotent_when_valid(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    result = normalize_viewbox(doc_id)
    svg_root = _working_tree(root, doc_id)
    assert svg_root.get("viewBox") == "0 0 100 100"
    assert "already normalized" in (result.summary or "")


# --- honest `changed` flag ----------------------------------------


def test_normalize_viewbox_already_normal_reports_changed_false(
    doc: tuple[str, Path, Path],
) -> None:
    # The fixture already carries a valid viewBox; normalizing is a genuine no-op. The pipeline
    # must report changed=False AND write NO snapshot and NO Operation Record (counts unchanged).
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    before_working = working.read_bytes()
    ops_before = _op_count(root, doc_id)
    snaps_before = _snapshot_count(root, doc_id)

    result = normalize_viewbox(doc_id)

    assert result.changed is False
    assert result.operation_id == ""
    assert result.snapshot_id == ""
    # No junk in the audit trail / snapshot list, and the working copy is byte-unchanged.
    assert _op_count(root, doc_id) == ops_before
    assert _snapshot_count(root, doc_id) == snaps_before
    assert working.read_bytes() == before_working


def test_real_mutation_writes_exactly_one_snapshot_and_record(
    doc: tuple[str, Path, Path],
) -> None:
    # A genuine change writes EXACTLY one snapshot + one Operation Record with changed=True.
    doc_id, root, _ = doc
    ops_before = _op_count(root, doc_id)
    snaps_before = _snapshot_count(root, doc_id)

    result = move_object(doc_id, "r1", 5.0, -3.0)

    assert result.changed is True
    assert result.operation_id.startswith("op_")
    assert result.snapshot_id.startswith("snap_")
    assert _op_count(root, doc_id) == ops_before + 1
    assert _snapshot_count(root, doc_id) == snaps_before + 1
    record = _operation_record(root, doc_id, result.operation_id)
    assert record["status"] == "applied"


def test_normalize_viewbox_synthesize_reports_changed_true(
    doc_no_viewbox: tuple[str, Path, Path],
) -> None:
    # Synthesizing an absent viewBox is a real content change.
    doc_id, _, _ = doc_no_viewbox
    result = normalize_viewbox(doc_id)
    assert result.changed is True


def test_move_object_real_change_reports_changed_true(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, _ = doc
    result = move_object(doc_id, "r1", 5.0, -3.0)
    assert result.changed is True


# --- resize_canvas adjust_viewbox ---------------------------------


def test_resize_canvas_adjust_viewbox_retargets(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    result = resize_canvas(doc_id, "300px", "120px", adjust_viewbox=True)
    svg_root = _working_tree(root, doc_id)
    assert svg_root.get("width") == "300px"
    assert svg_root.get("height") == "120px"
    # The viewBox now tracks the new canvas, replacing the old "0 0 100 100".
    assert svg_root.get("viewBox") == "0 0 300 120"
    assert result.changed is True


def test_resize_canvas_default_preserves_viewbox(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    resize_canvas(doc_id, "300px", "120px")
    svg_root = _working_tree(root, doc_id)
    # Default behaviour leaves an existing valid viewBox untouched.
    assert svg_root.get("viewBox") == "0 0 100 100"


def test_resize_canvas_adjust_viewbox_reversible(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    pre = working.read_bytes()
    result = resize_canvas(doc_id, "300px", "120px", adjust_viewbox=True)
    assert working.read_bytes() != pre
    restore_snapshot(doc_id, result.snapshot_id)
    assert working.read_bytes() == pre


# --- fit_to_content (Inkscape engine) ------------------------------


@pytest.mark.inkscape
def test_fit_to_content_sets_viewbox_to_content_bbox(
    doc_offset_content: tuple[str, Path, Path],
) -> None:
    doc_id, root, _ = doc_offset_content
    result = fit_to_content(doc_id)
    svg_root = _working_tree(root, doc_id)
    # The engine-computed content bbox for the single offset rect is "40 30 20 50".
    assert svg_root.get("viewBox") == "40 30 20 50"
    assert result.changed is True


@pytest.mark.inkscape
def test_fit_to_content_reversible(doc_offset_content: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc_offset_content
    working = sandbox.working_copy(root, doc_id)
    pre = working.read_bytes()
    result = fit_to_content(doc_id)
    assert working.read_bytes() != pre
    restore_snapshot(doc_id, result.snapshot_id)
    assert working.read_bytes() == pre


@pytest.mark.inkscape
def test_fit_to_content_is_idempotent_second_call_is_noop(
    doc_offset_content: tuple[str, Path, Path],
) -> None:
    # First fit retargets the viewBox (a real change); the second fit on the now-fitted document
    # must be a genuine no-op — byte-identical working copy, changed=False, and no new snapshot.
    doc_id, root, _ = doc_offset_content
    working = sandbox.working_copy(root, doc_id)

    first = fit_to_content(doc_id)
    assert first.changed is True
    after_first = working.read_bytes()
    snaps_after_first = _snapshot_count(root, doc_id)
    ops_after_first = _op_count(root, doc_id)

    second = fit_to_content(doc_id)

    assert second.changed is False
    assert second.operation_id == ""
    assert second.snapshot_id == ""
    # The working copy is byte-identical — no re-fit happened.
    assert working.read_bytes() == after_first
    # No new snapshot or Operation Record was written for the no-op second call.
    assert _snapshot_count(root, doc_id) == snaps_after_first
    assert _op_count(root, doc_id) == ops_after_first


# --- tile (b) --------------------------------------------------------


def test_tile_produces_grid_in_one_reversible_call(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    pre = working.read_bytes()

    result = tile(doc_id, "r1", rows=4, cols=4, dx=20.0, dy=20.0)

    # A 4x4 grid: the original plus 15 clones = 16 rects, all in ONE operation.
    rects = _working_tree(root, doc_id).findall(f".//{{{SVG_NS}}}rect")
    assert len(rects) == 16
    assert result.changed is True
    # One Operation Record / one snapshot covers the whole grid; reversible to the pre-tile state.
    record = _operation_record(root, doc_id, result.operation_id)
    assert record["status"] == "applied"
    assert record["risk_class"] == "medium"
    restore_snapshot(doc_id, result.snapshot_id)
    assert working.read_bytes() == pre


def test_tile_offsets_clones_by_grid_step(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    tile(doc_id, "r1", rows=1, cols=2, dx=25.0, dy=0.0)
    rects = _working_tree(root, doc_id).findall(f".//{{{SVG_NS}}}rect")
    # The single clone (cell (0,1)) carries a translate of (1*25, 0).
    transforms = [r.get("transform") for r in rects if r.get("transform")]
    assert "translate(25,0)" in transforms


def test_tile_1x1_is_noop_reports_changed_false(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    result = tile(doc_id, "r1", rows=1, cols=1, dx=10.0, dy=10.0)
    rects = _working_tree(root, doc_id).findall(f".//{{{SVG_NS}}}rect")
    assert len(rects) == 1
    assert result.changed is False


def test_tile_rejects_nonpositive_counts(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()
    with pytest.raises(ToolError):
        tile(doc_id, "r1", rows=0, cols=3, dx=1.0, dy=1.0)
    assert working.read_bytes() == before


def test_tile_rejects_oversized_grid(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()
    with pytest.raises(ToolError):
        tile(doc_id, "r1", rows=100, cols=100, dx=1.0, dy=1.0)
    assert working.read_bytes() == before


def test_tile_missing_object_maps_to_toolerror(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, _ = doc
    with pytest.raises(ToolError) as exc:
        tile(doc_id, "nope", rows=2, cols=2, dx=1.0, dy=1.0)
    assert "object id not found in document" in str(exc.value)


# --- reversibility ----------------------------------------------------------


def test_reversibility_restore_snapshot_returns_pre_edit_bytes(
    doc: tuple[str, Path, Path],
) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    pre_edit_bytes = working.read_bytes()

    result = move_object(doc_id, "r1", 5.0, 5.0)
    assert working.read_bytes() != pre_edit_bytes

    restore_snapshot(doc_id, result.snapshot_id)
    assert working.read_bytes() == pre_edit_bytes


# --- error mapping ----------------------------------------------------------


def test_unknown_doc_id_maps_to_toolerror(doc: tuple[str, Path, Path]) -> None:
    with pytest.raises(ToolError) as exc:
        move_object("d_nope", "r1", 1.0, 1.0)
    assert "document id not found" in str(exc.value)


def test_missing_object_id_maps_to_toolerror(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, _ = doc
    with pytest.raises(ToolError) as exc:
        move_object(doc_id, "does-not-exist", 1.0, 1.0)
    assert "object id not found in document" in str(exc.value)


# --- registration -----------------------------------------------------------


def test_tools_registered_on_mcp(doc: tuple[str, Path, Path]) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {
        "move_object",
        "scale_object",
        "rotate_object",
        "resize_canvas",
        "normalize_viewbox",
        "fit_to_content",
        "tile",
    } <= names
