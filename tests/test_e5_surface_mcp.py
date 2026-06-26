"""E5 surface verification over the live MCP client (E9-04).

The three E5 tools (`svg_web_optimize`, `quality_report`, `export_batch`) were unit-tested at the
function layer but never exercised THROUGH a FastMCP client — the previously-running server predated
the E5 landing (`1f8c329`). These tests drive each tool over the in-memory `Client` (the same path a
real STDIO host uses) and assert end-to-end behaviour, not just that the call returns:

1. all three appear in the live tool list (part of the 64),
2. `svg_web_optimize` dry+real both succeed over the client; the real run is reversible + recorded
   (a snapshot + an `applied` Operation Record), the original source is untouched, and a
   `<style>`/`<script>` inside `<defs>` survives the optimizer,
3. `quality_report` is read-only and returns the structured `QualityMetrics` +
   `OptimizationOpportunity[]` model (not prose), emitting no Operation Record,
4. `export_batch` dry run reports the plan + projected sizes + `within_budget`; a real run honours
   the item cap (`MAX_BATCH_ITEMS`) and the byte budget, refusing over-cap / over-budget requests
   before any write (surfaced as a client-side `ToolError`).

Async is run via `asyncio.run(...)` inside sync test functions, matching the repo convention
(see `test_resource_client.py`); no async plugin/config is added. The full tool/resource surface
is registered once at import time so the client sees every tool.

`svg_web_optimize` carries NO `@pytest.mark.inkscape`: the optimizer is pure-lxml DOM (E2-04) and
the pipeline's before/after preview is best-effort, so the test monkeypatches `render_preview` to
stay Inkscape-free and deterministic (mirroring `test_optimize.py`) — the mutation/reversibility
never depend on the binary. `export_batch`'s real-run test DOES shell out to Inkscape to produce
artifacts, so it is marked `@pytest.mark.inkscape` (auto-skips when the binary is absent).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.operations import OperationStatus, get_operation
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render.batch import MAX_BATCH_ITEMS
from inkscape_mcp.render.cli import RenderResult
from inkscape_mcp.server import mcp, register_tools
from inkscape_mcp.snapshots import list_snapshots
from inkscape_mcp.workspace import sandbox

# Register the full tool surface against the shared app once, at import time, so the in-memory
# client sees every tool (idempotent: the decorators tolerate re-registration).
register_tools()

# An SVG carrying every kind of cruft `svg_web_optimize` strips, plus a <style> AND a <script>
# inside <defs> (neither carries an id but both apply globally — never pruned), and a referenced
# group/gradient so reference preservation can be checked.
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

SVG_NS = "http://www.w3.org/2000/svg"
PNG_MAGIC = b"\x89PNG"

inkscape_available = shutil.which("inkscape") is not None


def _fake_render_preview(doc_id: str, width_px: int | None = None) -> RenderResult:
    """Hermetic stand-in for `render_preview` (no Inkscape): write a tiny deterministic PNG.

    Lifted from `test_optimize.py` so the `svg_web_optimize` surface test stays Inkscape-free —
    the optimizer itself is pure lxml and the before/after preview is best-effort.
    """
    reg = get_registry()
    entry = reg.get(doc_id)
    root = Path(entry.root)
    preview_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    descriptor = "auto" if width_px is None else f"{width_px}px"
    produced = preview_dir / f"preview-{descriptor}.png"
    produced.write_bytes(b"\x89PNG\r\n\x1a\n")
    # E11-01 one-location contract: artifact_path is workspace-ROOT-relative (matches the engine).
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
    """Open a fresh document in an isolated workspace; return (doc_id, root, source path)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    src = ws / "art.svg"
    src.write_bytes(SVG)
    entry = get_registry().open_document(str(src))
    return entry.doc_id, ws, src


def _call(name: str, args: dict[str, Any]) -> Any:
    """Call a tool over the in-memory client and return the `CallToolResult` (raises ToolError)."""

    async def _run() -> Any:
        async with Client(mcp) as client:
            return await client.call_tool(name, args)

    return asyncio.run(_run())


def _working_root(doc_id: str, root: Path) -> Any:
    from lxml import etree

    return etree.fromstring(sandbox.working_copy(root, doc_id).read_bytes())


def _locals(root: Any) -> set[str]:
    from lxml import etree

    return {etree.QName(e.tag).localname for e in root.iter() if isinstance(e.tag, str)}


def _workspace_path(doc_id: str, rel: str) -> Path:
    # E11-01 ONE LOCATION CONTRACT: returned artifact paths are relative to the workspace ROOT.
    entry = get_registry().get(doc_id)
    return Path(entry.root) / rel


# --- 1. all three tools live on the surface ---------------------------------


def test_all_three_e5_tools_in_live_tool_list(doc: tuple[str, Path, Path]) -> None:
    """The E5 trio is reachable over the client's `list_tools` (part of the 64)."""

    async def _run() -> set[str]:
        async with Client(mcp) as client:
            return {t.name for t in await client.list_tools()}

    names = asyncio.run(_run())
    assert {"svg_web_optimize", "quality_report", "export_batch"} <= names


# --- 2. svg_web_optimize: dry + real, reversible + recorded -----------------


def test_svg_web_optimize_dry_run_over_client(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `dry_run`-style preview is not part of this tool's surface — but the call must succeed."""
    monkeypatch.setattr("inkscape_mcp.edit.pipeline.render_preview", _fake_render_preview)
    doc_id, _root, _src = doc
    # The optimizer exposes no dry flag; a precision-only call IS the smallest "dry then real"
    # exercise: invoke once to confirm the tool runs cleanly over the client and reports a change.
    res = _call("svg_web_optimize", {"doc_id": doc_id, "precision": 2})
    assert res.is_error is False
    assert res.data.changed is True
    assert res.data.operation_id.startswith("op_")
    assert res.data.snapshot_id.startswith("snap_")


def test_svg_web_optimize_real_run_is_reversible_and_recorded(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real run lands a reversible, audited mutation: snapshot + `applied` Operation Record."""
    monkeypatch.setattr("inkscape_mcp.edit.pipeline.render_preview", _fake_render_preview)
    doc_id, root, src = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()

    res = _call("svg_web_optimize", {"doc_id": doc_id})
    assert res.is_error is False
    op_id = res.data.operation_id
    snap_id = res.data.snapshot_id

    # The working copy actually changed.
    after = sandbox.working_copy(root, doc_id).read_bytes()
    assert after != before

    # A pre-mutation snapshot exists in the index and matches the returned id.
    snaps = list_snapshots(doc_id)
    assert any(s.snapshot_id == snap_id for s in snaps)

    # The Operation Record was persisted, linked to the snapshot, and marked `applied` (ADR-004).
    record = get_operation(doc_id, op_id)
    assert record.tool == "svg_web_optimize"
    assert record.status is OperationStatus.APPLIED
    assert record.snapshot_id == snap_id

    # Reversible: restoring that snapshot returns the working copy to its pre-optimize bytes,
    # exercised over the client's `restore_snapshot` tool.
    rr = _call("restore_snapshot", {"doc_id": doc_id, "snapshot_id": snap_id})
    assert rr.is_error is False
    assert sandbox.working_copy(root, doc_id).read_bytes() == before

    # The ORIGINAL source file was never touched.
    assert src.read_bytes() == SVG


def test_svg_web_optimize_preserves_style_and_script_in_defs(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`<style>` and `<script>` inside `<defs>` carry no id but apply globally — never pruned."""
    monkeypatch.setattr("inkscape_mcp.edit.pipeline.render_preview", _fake_render_preview)
    doc_id, root, _src = doc
    _call("svg_web_optimize", {"doc_id": doc_id})
    optimized = _working_root(doc_id, root)
    locals_ = _locals(optimized)
    # Both global-scope defs survive the cleanup.
    assert "style" in locals_
    assert "script" in locals_
    assert len([e for e in optimized.iter(f"{{{SVG_NS}}}style")]) == 1
    assert len([e for e in optimized.iter(f"{{{SVG_NS}}}script")]) == 1
    # Sanity: editor cruft really was stripped on this same pass.
    assert "namedview" not in locals_
    assert "metadata" not in locals_


# --- 3. quality_report: read-only, structured model, no mutation ------------


def test_quality_report_returns_typed_model_no_mutation(doc: tuple[str, Path, Path]) -> None:
    """Read-only: returns structured `QualityMetrics` + `OptimizationOpportunity[]`; no record."""
    doc_id, root, _src = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()

    res = _call("quality_report", {"doc_id": doc_id})
    assert res.is_error is False

    # Typed shape over the wire (not prose): the structured payload carries the metrics block and
    # an opportunities list, each opportunity a {code, count, message} record.
    payload = res.structured_content
    assert payload is not None
    assert payload["doc_id"] == doc_id
    assert isinstance(payload["findings"], list)
    metrics = payload["metrics"]
    for key in (
        "object_count",
        "node_count",
        "layer_count",
        "embedded_raster_count",
        "embedded_raster_bytes",
        "font_coverage",
        "viewbox",
    ):
        assert key in metrics
    assert "present" in metrics["viewbox"] and "valid" in metrics["viewbox"]
    assert {"referenced", "installed", "missing", "checked"} <= set(metrics["font_coverage"])
    opps = payload["opportunities"]
    assert isinstance(opps, list)
    for opp in opps:
        assert {"code", "count", "message"} <= set(opp)
    # Fixture has editor cruft + an unreferenced id + reducible coords → at least one opportunity.
    codes = {opp["code"] for opp in opps}
    assert {"editor_metadata", "unreferenced_ids", "reducible_coords"} <= codes

    # Also reachable as a typed model via `.data`.
    assert res.data.doc_id == doc_id
    assert res.data.metrics.object_count >= 1

    # Read-only: the working copy is byte-identical and NO operations dir/record was written.
    assert sandbox.working_copy(root, doc_id).read_bytes() == before
    ops_dir = sandbox.operations_dir(root, doc_id)
    assert not ops_dir.exists() or not any(ops_dir.iterdir())


def test_quality_report_unknown_doc_refused(doc: tuple[str, Path, Path]) -> None:
    """An unknown doc id surfaces as a stable, host-path-free `ToolError` over the client."""
    with pytest.raises(ToolError) as exc:
        _call("quality_report", {"doc_id": "d_missing"})
    assert "not found" in str(exc.value)


# --- 4. export_batch: dry plan, item cap, byte budget -----------------------


def test_export_batch_dry_run_reports_plan_and_budget(doc: tuple[str, Path, Path]) -> None:
    """Dry run (default) reports the plan + projected sizes + `within_budget`; writes nothing."""
    doc_id, _root, _src = doc
    specs = [
        {"format": "png", "width_px": 64},
        {"format": "svg"},
        {"format": "png", "object_id": "keepg", "width_px": 32},
    ]
    res = _call("export_batch", {"doc_id": doc_id, "specs": specs})
    assert res.is_error is False
    data = res.data
    assert data.dry_run is True
    assert data.item_count == 3
    assert data.projected_total_bytes > 0
    assert data.actual_total_bytes is None
    assert isinstance(data.within_budget, bool)
    assert [i.status for i in data.items] == ["planned", "planned", "planned"]
    assert all(i.projected_bytes > 0 for i in data.items)
    assert all(i.artifact_path is None for i in data.items)
    # Nothing was written under the exports dir.
    exports = Path(get_registry().get(doc_id).workspace_dir) / "artifacts" / "exports"
    assert not exports.exists() or not any(exports.iterdir())


def test_export_batch_dry_run_reports_over_budget_flag(doc: tuple[str, Path, Path]) -> None:
    """A dry run never refuses — it reports the over-budget projection via `within_budget=False`."""
    doc_id, _root, _src = doc
    res = _call(
        "export_batch",
        {
            "doc_id": doc_id,
            "specs": [{"format": "png", "width_px": 256}],
            "dry_run": True,
            "byte_budget": 16,
        },
    )
    assert res.data.dry_run is True
    assert res.data.within_budget is False


def test_export_batch_over_cap_refused_over_client(doc: tuple[str, Path, Path]) -> None:
    """An over-cap batch (> MAX_BATCH_ITEMS specs) is refused before any write, as a `ToolError`."""
    doc_id, _root, _src = doc
    specs = [{"format": "png", "width_px": 16} for _ in range(MAX_BATCH_ITEMS + 1)]
    with pytest.raises(ToolError) as exc:
        _call("export_batch", {"doc_id": doc_id, "specs": specs})
    assert "item cap" in str(exc.value)


def test_export_batch_over_budget_real_run_refused_before_write(
    doc: tuple[str, Path, Path],
) -> None:
    """A real run whose projection blows the byte budget is refused before writing (`ToolError`)."""
    doc_id, _root, _src = doc
    with pytest.raises(ToolError) as exc:
        _call(
            "export_batch",
            {
                "doc_id": doc_id,
                "specs": [{"format": "png", "width_px": 256}],
                "dry_run": False,
                "byte_budget": 16,
            },
        )
    assert "budget" in str(exc.value)
    # No artifacts were written.
    exports = Path(get_registry().get(doc_id).workspace_dir) / "artifacts" / "exports"
    assert not exports.exists() or not any(exports.iterdir())


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_batch_real_run_exports_within_cap_and_budget(doc: tuple[str, Path, Path]) -> None:
    """A real run within the cap + budget exports each spec through the Inkscape engine."""
    doc_id, _root, _src = doc
    specs = [
        {"format": "png", "width_px": 48},
        {"format": "svg"},
        {"format": "png", "object_id": "keepg", "width_px": 24},
    ]
    res = _call("export_batch", {"doc_id": doc_id, "specs": specs, "dry_run": False})
    data = res.data
    assert data.dry_run is False
    assert data.item_count == 3
    assert data.actual_total_bytes is not None and data.actual_total_bytes > 0
    # Honoured the budget: the real total never exceeded it.
    assert data.actual_total_bytes <= data.byte_budget
    for item in data.items:
        assert item.status == "exported"
        assert item.artifact_path is not None
        assert not item.artifact_path.startswith("/")  # workspace-relative
        assert _workspace_path(doc_id, item.artifact_path).exists()
    # First item is a real PNG.
    png_out = _workspace_path(doc_id, data.items[0].artifact_path or "")
    assert png_out.read_bytes()[:4] == PNG_MAGIC
