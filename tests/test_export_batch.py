"""Batch-export tool + engine tests (E5-06).

The dry-run / cap / budget / validation tests need NO Inkscape (no writes; everything is checked
before any invocation). The real-run test that actually produces artifacts is marked
`@pytest.mark.inkscape` and runs the local binary (auto-skips when absent).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render.batch import MAX_BATCH_ITEMS, ExportSpec
from inkscape_mcp.server import mcp
from inkscape_mcp.tools.export_batch import export_batch

SVG = (
    b'<?xml version="1.0"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="80" viewBox="0 0 100 80">'
    b'<rect id="bg" x="0" y="0" width="100" height="80" fill="#ffffff"/>'
    b'<circle id="dot" cx="30" cy="30" r="20" fill="#ff0000"/>'
    b"</svg>"
)

PNG_MAGIC = b"\x89PNG"

inkscape_available = shutil.which("inkscape") is not None


@pytest.fixture
def doc_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    src = ws / "art.svg"
    src.write_bytes(SVG)
    return get_registry().open_document(str(src)).doc_id


def _root_join(doc_id: str, ws_rel: str) -> Path:
    """Join a returned path (`artifact_path` or `workspace_relative_path`) to the workspace ROOT.

    ONE LOCATION CONTRACT (E11-01): both fields are root-relative and identical, so a single plain
    join to the workspace ROOT opens any artifact — no `find`/`stat`.
    """
    entry = get_registry().get(doc_id)
    return Path(entry.root) / ws_rel


# --- registration -----------------------------------------------------------


def test_registered_on_mcp(doc_id: str) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "export_batch" in names


# --- dry run is the default and writes nothing ------------------------------


def test_dry_run_default_plans_without_writing(doc_id: str) -> None:
    specs = [
        ExportSpec(format="png", width_px=64),
        ExportSpec(format="svg"),
        ExportSpec(format="png", object_id="dot", width_px=32),
    ]
    result = export_batch(doc_id, specs)
    assert result.dry_run is True
    assert result.item_count == 3
    assert result.projected_total_bytes > 0
    assert result.actual_total_bytes is None
    # Every item is "planned" with a projected size and no artifact path.
    assert [i.status for i in result.items] == ["planned", "planned", "planned"]
    assert all(i.artifact_path is None for i in result.items)
    # Nothing was written under the exports dir.
    exports = Path(get_registry().get(doc_id).workspace_dir) / "artifacts" / "exports"
    assert not exports.exists() or not any(exports.iterdir())


# --- bounds -----------------------------------------------------------------


def test_empty_batch_refused(doc_id: str) -> None:
    with pytest.raises(ToolError) as exc:
        export_batch(doc_id, [])
    assert "at least one" in str(exc.value)


def test_item_cap_enforced(doc_id: str) -> None:
    specs = [ExportSpec(format="png", width_px=16) for _ in range(MAX_BATCH_ITEMS + 1)]
    with pytest.raises(ToolError) as exc:
        export_batch(doc_id, specs)
    assert "item cap" in str(exc.value)


def test_real_run_refuses_over_budget(doc_id: str) -> None:
    # A tiny budget the conservative PNG projection (w*h*4) blows past — refused before any write.
    with pytest.raises(ToolError) as exc:
        export_batch(
            doc_id, [ExportSpec(format="png", width_px=256)], dry_run=False, byte_budget=16
        )
    assert "budget" in str(exc.value)


def test_dry_run_reports_within_budget_flag(doc_id: str) -> None:
    result = export_batch(
        doc_id, [ExportSpec(format="png", width_px=256)], dry_run=True, byte_budget=16
    )
    # Dry run never refuses — it reports the over-budget projection.
    assert result.dry_run is True
    assert result.within_budget is False


def test_byte_budget_clamped_to_artifact_cap(doc_id: str) -> None:
    cap = get_settings().artifact_max_bytes_per_doc
    # A caller may only TIGHTEN the budget; an oversized value is clamped down to the per-doc cap.
    result = export_batch(doc_id, [ExportSpec(format="png", width_px=16)], byte_budget=cap * 100)
    assert result.byte_budget == cap


# --- spec validation (no Inkscape) ------------------------------------------


def test_bad_format_refused(doc_id: str) -> None:
    with pytest.raises(ToolError) as exc:
        export_batch(doc_id, [ExportSpec(format="gif")])
    assert "format" in str(exc.value)


def test_nonpositive_width_refused(doc_id: str) -> None:
    with pytest.raises(ToolError) as exc:
        export_batch(doc_id, [ExportSpec(format="png", width_px=0)])
    assert "width_px" in str(exc.value)


def test_missing_object_id_refused(doc_id: str) -> None:
    with pytest.raises(ToolError) as exc:
        export_batch(doc_id, [ExportSpec(format="png", object_id="nope")])
    assert "object id not found" in str(exc.value)


def test_unknown_doc_id(doc_id: str) -> None:
    with pytest.raises(ToolError) as exc:
        export_batch("d_missing", [ExportSpec(format="png")])
    assert "not found" in str(exc.value)


# --- real run produces artifacts (needs Inkscape) ---------------------------


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_real_run_exports_each_spec(doc_id: str) -> None:
    specs = [
        ExportSpec(format="png", width_px=48),
        ExportSpec(format="svg"),
        ExportSpec(format="png", object_id="dot", width_px=24),
    ]
    result = export_batch(doc_id, specs, dry_run=False)
    assert result.dry_run is False
    assert result.item_count == 3
    assert result.actual_total_bytes is not None and result.actual_total_bytes > 0
    for item in result.items:
        assert item.status == "exported"
        assert item.artifact_path is not None
        # ONE LOCATION CONTRACT (E11-01): artifact_path == workspace_relative_path, root-relative.
        assert not item.artifact_path.startswith("/")
        assert item.artifact_path == item.workspace_relative_path
        out = _root_join(doc_id, item.artifact_path)
        assert out.exists()
    # First item is a PNG, second an SVG.
    png_out = _root_join(doc_id, result.items[0].artifact_path or "")
    assert png_out.read_bytes()[:4] == PNG_MAGIC


# --- E11-01: each batch item carries a resolvable location -------------------


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_real_run_items_resolvable_by_root_join(doc_id: str) -> None:
    specs = [ExportSpec(format="png", width_px=32), ExportSpec(format="svg")]
    result = export_batch(doc_id, specs, dry_run=False)
    for item in result.items:
        assert item.workspace_relative_path is not None
        # ONE LOCATION CONTRACT (E11-01): artifact_path == workspace_relative_path, both open via a
        # single join to the workspace ROOT.
        assert item.artifact_path == item.workspace_relative_path
        assert item.workspace_relative_path.startswith(f".inkscape-mcp/documents/{doc_id}/")
        assert _root_join(doc_id, item.workspace_relative_path).exists()
        assert _root_join(doc_id, item.artifact_path or "").exists()


# --- E11-05: out_dir sandbox check + back-compat -----------------------------


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_real_run_out_dir_in_workspace(doc_id: str) -> None:
    root = Path(get_registry().get(doc_id).root)
    (root / "batchout").mkdir()
    result = export_batch(
        doc_id,
        [ExportSpec(format="png", width_px=32)],
        dry_run=False,
        out_dir="batchout",
        name_prefix="b",
    )
    item = result.items[0]
    assert item.workspace_relative_path is not None
    # ONE LOCATION CONTRACT (E11-01): for an out_dir output too, both fields are the same
    # in-workspace relative path and open via a single root join.
    assert item.artifact_path == item.workspace_relative_path
    assert item.workspace_relative_path.startswith("batchout/")
    assert _root_join(doc_id, item.workspace_relative_path).exists()
    assert _root_join(doc_id, item.artifact_path or "").exists()


def test_out_dir_outside_workspace_rejected(doc_id: str) -> None:
    # E11-05 / sec.12: rejected up front (even though dry_run default would write nothing) with the
    # stable sandbox message and no host path.
    with pytest.raises(ToolError) as exc:
        export_batch(doc_id, [ExportSpec(format="png", width_px=16)], out_dir="../escape")
    assert "outside workspace" in str(exc.value)


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_out_dir_omitted_back_compat(doc_id: str) -> None:
    # Omitting out_dir keeps the managed per-doc exports dir.
    result = export_batch(doc_id, [ExportSpec(format="png", width_px=16)], dry_run=False)
    assert "artifacts/exports/" in (result.items[0].artifact_path or "")


# --- E14-08a: capability-absent batch errors name discovery tools -------------


def test_batch_map_failure_missing_engine_names_list_capabilities() -> None:
    from inkscape_mcp.tools.export_batch import _map_failure
    from inkscape_mcp.workspace.subprocess_exec import ProcessError

    msg = str(_map_failure(ProcessError("inkscape binary not found")))
    assert "list_capabilities" in msg
    assert "binary not found" not in msg  # host-path/detail-free (sec.12)


def test_batch_unsupported_format_names_list_capabilities() -> None:
    # E14-08a: a spec with an unsupported format is refused with a message naming list_capabilities.
    from inkscape_mcp.config import get_settings
    from inkscape_mcp.render.batch import BatchError, ExportSpec, _validate_spec

    with pytest.raises(BatchError) as exc:
        _validate_spec(ExportSpec(format="gif"), 0, get_settings(), None)
    assert "list_capabilities" in str(exc.value)
