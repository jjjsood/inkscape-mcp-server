"""Cross-document / collection layer tests.

Covers the four collection tools:

- `compose_grid` — lays N DISTINCT docs/objects into a grid in one call (cell count + placement +
  one snapshot/Operation Record).
- `export_set` / `optimize_set` / `quality_report_set` — per-doc results + aggregate + a structured
  cross-doc CONSISTENCY VERDICT; the verdict flags the disagreeing `doc_ids` on a deliberate
  viewBox / stroke mismatch. Mutating set ops write one snapshot per affected doc; the read-only set
  op writes none.

Hermetic: `render_preview` is monkeypatched in the pipeline so no test invokes Inkscape.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from lxml import etree

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.edit import pipeline
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render.batch import ExportSpec
from inkscape_mcp.render.cli import RenderResult
from inkscape_mcp.server import mcp
from inkscape_mcp.tools.compose import compose_grid
from inkscape_mcp.tools.export_batch import export_set
from inkscape_mcp.tools.optimize import optimize_set
from inkscape_mcp.tools.quality import quality_report_set
from inkscape_mcp.workspace import sandbox

SVG_NS = "http://www.w3.org/2000/svg"
PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-preview"


#: Three distinct, mutually-CONSISTENT assets (same 100x100 canvas, same stroke-width, kebab ids).
def _asset(oid: str, fill: str) -> bytes:
    return (
        f'<svg xmlns="{SVG_NS}" width="100" height="100" viewBox="0 0 100 100">'
        f'<rect id="{oid}" x="10" y="10" width="40" height="40" '
        f'stroke="#000" stroke-width="2" fill="{fill}"/></svg>'
    ).encode()


#: A document that DISAGREES on viewBox (200x200) and stroke-width (5) — used to assert the verdict.
SVG_MISMATCH = (
    f'<svg xmlns="{SVG_NS}" width="200" height="200" viewBox="0 0 200 200">'
    f'<rect id="odd_box" x="10" y="10" width="40" height="40" '
    f'stroke="#000" stroke-width="5" fill="#00f"/></svg>'
).encode()


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


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    return ws


def _open(ws: Path, name: str, data: bytes) -> str:
    src = ws / name
    src.write_bytes(data)
    return get_registry().open_document(str(src)).doc_id


def _consistent_set(ws: Path) -> list[str]:
    """Three distinct but mutually-consistent documents."""
    return [
        _open(ws, "a.svg", _asset("icon_a", "#f00")),
        _open(ws, "b.svg", _asset("icon_b", "#0f0")),
        _open(ws, "c.svg", _asset("icon_c", "#00f")),
    ]


def _working_root(doc_id: str) -> etree._Element:
    entry = get_registry().get(doc_id)
    return etree.parse(str(sandbox.working_copy(Path(entry.root), doc_id))).getroot()


def _snapshot_count(doc_id: str) -> int:
    entry = get_registry().get(doc_id)
    snap_dir = sandbox.snapshots_dir(Path(entry.root), doc_id)
    return len(list(snap_dir.glob("*.svg"))) if snap_dir.exists() else 0


def _op_count(doc_id: str) -> int:
    entry = get_registry().get(doc_id)
    op_dir = sandbox.operations_dir(Path(entry.root), doc_id)
    return len(list(op_dir.glob("op_*.json"))) if op_dir.exists() else 0


# --- registration -----------------------------------------------------------


def test_collection_tools_registered() -> None:
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"compose_grid", "export_set", "optimize_set", "quality_report_set"} <= names


# --- compose_grid -----------------------------------------------------------


def test_compose_grid_lays_distinct_docs_into_grid(workspace: Path) -> None:
    docs = _consistent_set(workspace)

    result = compose_grid(rows=2, cols=2, cell=64, doc_ids=docs)

    # A new blank target document was created and carries the three cell groups.
    assert result.target_doc_id not in docs
    assert result.changed is True
    assert len(result.cells) == 3
    assert [(c.row, c.col) for c in result.cells] == [(0, 0), (0, 1), (1, 0)]
    assert [c.source for c in result.cells] == docs

    root = _working_root(result.target_doc_id)
    cell_groups = [
        g for g in root.iter(f"{{{SVG_NS}}}g") if (g.get("id") or "").startswith("cell-")
    ]
    assert len(cell_groups) == 3
    # Each cell group carries a translate transform placing it at its grid origin.
    transforms = [g.get("transform") or "" for g in cell_groups]
    assert all("translate(" in t for t in transforms)
    # Row-major: second cell is translated to col 1 (x = 64), third to row 1 (y = 64).
    assert "translate(64," in transforms[1]
    assert "translate(0,64)" in transforms[2]


def test_compose_grid_writes_one_snapshot_and_operation_record(workspace: Path) -> None:
    docs = _consistent_set(workspace)
    result = compose_grid(rows=1, cols=3, cell=50, doc_ids=docs)

    assert result.operation_id
    assert result.snapshot_id
    # The whole sheet lands under ONE snapshot + ONE Operation Record on the target document.
    assert _snapshot_count(result.target_doc_id) == 1
    assert _op_count(result.target_doc_id) == 1


def test_compose_grid_objects_mode_from_one_source(workspace: Path) -> None:
    src = (
        f'<svg xmlns="{SVG_NS}" width="100" height="100" viewBox="0 0 100 100">'
        f'<rect id="r1" x="0" y="0" width="10" height="10"/>'
        f'<circle id="c1" cx="50" cy="50" r="10"/></svg>'
    ).encode()
    doc_id = _open(workspace, "src.svg", src)

    result = compose_grid(rows=1, cols=2, cell=40, object_ids=["r1", "c1"], source_doc_id=doc_id)

    assert len(result.cells) == 2
    assert [c.source for c in result.cells] == ["r1", "c1"]
    # Source document is untouched (still exactly its two original objects, no clones).
    src_root = _working_root(doc_id)
    assert len(list(src_root.iter(f"{{{SVG_NS}}}rect"))) == 1
    assert len(list(src_root.iter(f"{{{SVG_NS}}}circle"))) == 1


def test_compose_grid_into_existing_target(workspace: Path) -> None:
    docs = _consistent_set(workspace)
    target = _open(
        workspace,
        "sheet.svg",
        f'<svg xmlns="{SVG_NS}" width="300" height="300" viewBox="0 0 300 300"/>'.encode(),
    )
    result = compose_grid(rows=1, cols=3, cell=80, doc_ids=docs, target_doc_id=target)
    assert result.target_doc_id == target
    assert _snapshot_count(target) == 1


def test_compose_grid_rejects_both_modes(workspace: Path) -> None:
    docs = _consistent_set(workspace)
    with pytest.raises(ToolError, match="EXACTLY ONE"):
        compose_grid(rows=2, cols=2, cell=64, doc_ids=docs, object_ids=["icon_a"])


def test_compose_grid_rejects_too_many_assets(workspace: Path) -> None:
    docs = _consistent_set(workspace)
    with pytest.raises(ToolError, match="assets but only"):
        compose_grid(rows=1, cols=2, cell=64, doc_ids=docs)  # 3 assets, 2 cells


def test_compose_grid_unknown_doc_id(workspace: Path) -> None:
    _consistent_set(workspace)
    with pytest.raises(ToolError, match="document id not found"):
        compose_grid(rows=1, cols=1, cell=64, doc_ids=["nope"])


# --- consistency verdict (shared shape) -------------------------------------


def test_quality_report_set_consistent_set(workspace: Path) -> None:
    docs = _consistent_set(workspace)
    result = quality_report_set(docs)

    assert len(result.per_doc) == 3
    assert result.consistency.consistent is True
    for prop in result.consistency.properties:
        assert prop.agree is True
    # Read-only: NO snapshot / Operation Record for any document.
    assert all(_snapshot_count(d) == 0 for d in docs)
    assert all(_op_count(d) == 0 for d in docs)


def test_quality_report_set_flags_mismatched_doc(workspace: Path) -> None:
    good = _consistent_set(workspace)
    odd = _open(workspace, "odd.svg", SVG_MISMATCH)
    docs = [*good, odd]

    result = quality_report_set(docs)

    assert result.consistency.consistent is False
    by_name = {p.property: p for p in result.consistency.properties}

    # viewBox disagrees: the odd doc is the lone "200x200", the good ones share "100x100".
    vb = by_name["viewBox"]
    assert vb.agree is False
    odd_bucket = next(ids for val, ids in vb.values.items() if "200" in val)
    assert odd_bucket == [odd]
    good_bucket = next(ids for val, ids in vb.values.items() if val == "100x100")
    assert set(good_bucket) == set(good)

    # stroke-width disagrees: the odd doc is "5", the good ones "2".
    sw = by_name["stroke_width"]
    assert sw.agree is False
    assert sw.values.get("5") == [odd]


def test_quality_report_set_rejects_empty_and_duplicates(workspace: Path) -> None:
    docs = _consistent_set(workspace)
    with pytest.raises(ToolError, match="at least one"):
        quality_report_set([])
    with pytest.raises(ToolError, match="duplicate"):
        quality_report_set([docs[0], docs[0]])


# --- export_set -------------------------------------------------------------


def test_export_set_aggregates_and_verdict(workspace: Path) -> None:
    docs = _consistent_set(workspace)
    specs = [ExportSpec(format="png", width_px=64)]

    result = export_set(docs, specs, dry_run=True)

    assert len(result.per_doc) == 3
    assert result.total_items == 3  # one spec per doc
    assert result.total_bytes == sum(e.result.projected_total_bytes for e in result.per_doc)
    assert result.consistency.consistent is True
    # Dry run: artifact-only tool, no snapshots regardless.
    assert all(_snapshot_count(d) == 0 for d in docs)


def test_export_set_flags_mismatch(workspace: Path) -> None:
    docs = [*_consistent_set(workspace), _open(workspace, "odd.svg", SVG_MISMATCH)]
    result = export_set(docs, [ExportSpec(format="png", width_px=64)], dry_run=True)
    assert result.consistency.consistent is False


# --- optimize_set -----------------------------------------------------------


def _bloated(oid: str) -> bytes:
    """A document with editor metadata + an empty group that `svg_web_optimize` will strip."""
    return (
        f'<svg xmlns="{SVG_NS}" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        f'width="100" height="100" viewBox="0 0 100 100">'
        f'<rect id="{oid}" x="10" y="10" width="40" height="40" '
        f'stroke="#000" stroke-width="2" fill="#f00" inkscape:label="junk"/>'
        f'<g id="empty_{oid}"></g></svg>'
    ).encode()


def test_optimize_set_aggregate_one_snapshot_per_changed_doc(workspace: Path) -> None:
    docs = [
        _open(workspace, "a.svg", _bloated("a")),
        _open(workspace, "b.svg", _bloated("b")),
    ]

    result = optimize_set(docs)

    assert len(result.per_doc) == 2
    assert result.changed_count == 2
    assert result.total_bytes_saved == result.total_bytes_before - result.total_bytes_after
    assert result.total_bytes_saved > 0
    # Mutating set op: exactly ONE snapshot + Operation Record per CHANGED document.
    for d in docs:
        assert _snapshot_count(d) == 1
        assert _op_count(d) == 1


def test_optimize_set_verdict_on_mismatch(workspace: Path) -> None:
    docs = [*_consistent_set(workspace), _open(workspace, "odd.svg", SVG_MISMATCH)]
    result = optimize_set(docs)
    assert result.consistency.consistent is False
    by_name = {p.property: p for p in result.consistency.properties}
    assert by_name["stroke_width"].agree is False
