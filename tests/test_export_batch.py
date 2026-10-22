"""Batch exports: `render.batch.export_batch` (engine) and its use by `export` file targets.

Several `{"kind": "file"}` targets of one document run as ONE bounded batch: item cap, a
conservative byte projection checked against `byte_budget` before anything is written, dry run.
The `inkscape` binary is faked (see `test_export_files.FakeInkscape`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import get_settings
from inkscape_mcp.document.inspect import DocumentNotFound
from inkscape_mcp.registry import get_registry
from inkscape_mcp.render.batch import MAX_BATCH_ITEMS, BatchError, ExportSpec, export_batch
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.paths import SandboxViolation
from tests.test_export_files import ART, FakeInkscape, install_fake
from tests.tool_support import call, open_doc

PNG_MAGIC = b"\x89PNG"
#: a 64 px PNG of the 100x80 page projects to 64*51*4 = 13 056 bytes
PNG_64 = {"kind": "file", "format": "png", "width_px": 64}


@pytest.fixture
def fake_inkscape(monkeypatch: pytest.MonkeyPatch, tool_ws: Path) -> FakeInkscape:
    return install_fake(monkeypatch)


@pytest.fixture
def doc(tool_ws: Path) -> str:
    return open_doc(tool_ws, "art.svg", ART)


def _written(doc_id: str) -> list[Path]:
    exports = sandbox.exports_dir(Path(get_registry().get(doc_id).root), doc_id)
    return list(exports.iterdir()) if exports.exists() else []


def _export(app, doc_id, targets, **kw):
    return call(app, "export", {"doc_id": doc_id, "targets": targets, **kw}).data


# --- engine -------------------------------------------------------------------------------


def test_engine_dry_run_is_the_default_and_writes_nothing(fake_inkscape, doc) -> None:
    specs = [
        ExportSpec(format="png", width_px=64),
        ExportSpec(format=" SVG "),  # format is normalised
        ExportSpec(format="png", object_id="dot", width_px=32),
    ]
    result = export_batch(doc, specs)
    assert result.dry_run is True and result.item_count == 3
    assert result.within_budget is True and result.actual_total_bytes is None
    assert [i.status for i in result.items] == ["planned"] * 3
    assert [i.format for i in result.items] == ["png", "svg", "png"]
    assert result.items[0].projected_bytes == 64 * 51 * 4  # RGBA upper bound
    assert result.projected_total_bytes == sum(i.projected_bytes for i in result.items)
    assert all(i.artifact_path is None for i in result.items)
    assert _written(doc) == [] and fake_inkscape.calls == []


def test_engine_real_run_exports_each_spec(fake_inkscape, doc) -> None:
    specs = [
        ExportSpec(format="png", width_px=48),
        ExportSpec(format="svg"),
        ExportSpec(format="png", object_id="dot", width_px=24),
    ]
    result = export_batch(doc, specs, dry_run=False)
    assert result.dry_run is False and result.item_count == 3
    assert 0 < (result.actual_total_bytes or 0) <= result.byte_budget
    root = Path(get_registry().get(doc).root)
    for item in result.items:
        assert item.status == "exported"
        assert item.artifact_path == item.workspace_relative_path
        assert item.workspace_relative_path.startswith(f".inkscape-mcp/documents/{doc}/")
        assert (root / item.workspace_relative_path).is_file()
    assert (root / result.items[0].workspace_relative_path).read_bytes()[:4] == PNG_MAGIC
    assert result.items[2].object_id == "dot"


def test_engine_out_dir_and_prefix_reach_every_export(fake_inkscape, doc, tool_ws) -> None:
    result = export_batch(
        doc,
        [ExportSpec(format="png", width_px=32), ExportSpec(format="svg")],
        dry_run=False,
        out_dir="batchout",
        name_prefix="b",
    )
    for item in result.items:
        assert item.workspace_relative_path.startswith("batchout/")
        assert (tool_ws / item.workspace_relative_path).is_file()
        assert "-b-" in item.workspace_relative_path


@pytest.mark.parametrize("dry_run", [True, False])
def test_engine_out_dir_escape_is_rejected_even_on_a_dry_run(fake_inkscape, doc, dry_run) -> None:
    with pytest.raises(SandboxViolation, match="outside workspace"):
        export_batch(doc, [ExportSpec(format="png", width_px=16)], dry_run=dry_run, out_dir="../x")
    assert fake_inkscape.calls == []


def test_engine_empty_and_oversized_batches_are_refused(fake_inkscape, doc) -> None:
    with pytest.raises(BatchError, match="at least one"):
        export_batch(doc, [])
    specs = [ExportSpec(format="png", width_px=16)] * (MAX_BATCH_ITEMS + 1)
    with pytest.raises(BatchError, match="item cap"):
        export_batch(doc, specs)
    export_batch(doc, specs[:MAX_BATCH_ITEMS])  # exactly at the cap is fine


def test_engine_over_budget_real_run_is_refused_before_any_write(fake_inkscape, doc) -> None:
    with pytest.raises(BatchError, match="budget"):
        export_batch(doc, [ExportSpec(format="png", width_px=256)], dry_run=False, byte_budget=16)
    assert fake_inkscape.calls == [] and _written(doc) == []


def test_engine_dry_run_reports_over_budget_instead_of_refusing(fake_inkscape, doc) -> None:
    result = export_batch(doc, [ExportSpec(format="png", width_px=256)], byte_budget=16)
    assert result.dry_run is True and result.within_budget is False and result.byte_budget == 16


def test_engine_budget_can_only_be_tightened(fake_inkscape, doc) -> None:
    cap = get_settings().artifact_max_bytes_per_doc
    result = export_batch(doc, [ExportSpec(format="png", width_px=16)], byte_budget=cap * 100)
    assert result.byte_budget == cap
    with pytest.raises(BatchError, match="positive"):
        export_batch(doc, [ExportSpec(format="png", width_px=16)], byte_budget=0)


def test_engine_aborts_when_real_output_outgrows_the_projection(fake_inkscape, doc) -> None:
    # An SVG projects as max(working size, 4096) but this one writes far more than budgeted.
    fake_inkscape.svg_body = b" " * 10_000
    with pytest.raises(BatchError, match="exceeded the byte budget mid-run"):
        export_batch(doc, [ExportSpec(format="svg")], dry_run=False, byte_budget=5000)


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (ExportSpec(format="gif"), r"spec 1: unsupported format"),
        (ExportSpec(format="png", width_px=0), "width_px must be a positive integer"),
        (ExportSpec(format="png", width_px=-4), "width_px must be a positive integer"),
        (ExportSpec(format="png", object_id="nope"), "object id not found"),
        (ExportSpec(format="png", object_id="a b"), "object id is not valid"),
    ],
)
def test_engine_invalid_specs_are_refused_up_front(fake_inkscape, doc, spec, message) -> None:
    ok = ExportSpec(format="png", width_px=16)
    with pytest.raises(BatchError, match=message):
        export_batch(doc, [ok, spec], dry_run=False)
    assert fake_inkscape.calls == [] and _written(doc) == []  # even the valid first spec


def test_engine_width_over_the_pixel_cap_is_refused(fake_inkscape, doc) -> None:
    too_wide = get_settings().max_export_px + 1
    with pytest.raises(BatchError, match="pixel cap"):
        export_batch(doc, [ExportSpec(format="png", width_px=too_wide)])


def test_engine_unknown_document(fake_inkscape, tool_ws) -> None:
    with pytest.raises(DocumentNotFound, match="not found"):
        export_batch("d_missing", [ExportSpec(format="png")])


# --- through the `export` tool ----------------------------------------------------------------


def test_tool_dry_run_plans_and_projects_without_writing(tool_app, fake_inkscape, doc) -> None:
    data = _export(
        tool_app,
        doc,
        [PNG_64, {"kind": "file", "format": "svg"}, {**PNG_64, "object_id": "dot", "width_px": 32}],
        dry_run=True,
    )
    assert data["dry_run"] is True and data["within_budget"] is True
    assert [p["format"] for p in data["plan"]] == ["png", "svg", "png"]
    assert data["plan"][2]["object_id"] == "dot"
    assert all(p["projected_bytes"] > 0 for p in data["plan"])
    assert data["projected_bytes"] == sum(p["projected_bytes"] for p in data["plan"])
    assert _written(doc) == [] and fake_inkscape.calls == []


def test_tool_dry_run_reports_over_budget_instead_of_refusing(tool_app, fake_inkscape, doc) -> None:
    data = _export(tool_app, doc, [{**PNG_64, "width_px": 256}], dry_run=True, byte_budget=16)
    assert data["dry_run"] is True and data["within_budget"] is False


def test_tool_dry_run_over_a_set_plans_per_document(tool_app, fake_inkscape, tool_ws) -> None:
    a, b = open_doc(tool_ws, "a.svg", ART), open_doc(tool_ws, "b.svg", ART)
    data = _export(tool_app, [a, b], [PNG_64], dry_run=True, byte_budget=20_000)
    assert set(data["plan"]) == {a, b}
    assert data["projected_bytes"] == 2 * 64 * 51 * 4
    # the budget is per document, so each doc's single PNG fits
    assert data["within_budget"] is True
    assert fake_inkscape.calls == []


def test_tool_over_the_item_cap_is_refused(tool_app, fake_inkscape, doc) -> None:
    targets = [{"kind": "file", "format": "png", "width_px": 16}] * (MAX_BATCH_ITEMS + 1)
    with pytest.raises(ToolError, match=r"targets\[0\] \(file\): .*item cap"):
        _export(tool_app, doc, targets)
    assert fake_inkscape.calls == [] and _written(doc) == []


def test_tool_over_budget_real_run_is_refused_before_any_write(
    tool_app, fake_inkscape, doc
) -> None:
    with pytest.raises(ToolError, match="budget"):
        _export(tool_app, doc, [{**PNG_64, "width_px": 256}], byte_budget=16)
    assert fake_inkscape.calls == [] and _written(doc) == []


def test_tool_budget_covers_all_file_targets_together(tool_app, fake_inkscape, doc) -> None:
    # one 64 px PNG projects to 13 056 B: it fits 20 000, two together do not
    assert _export(tool_app, doc, [PNG_64], byte_budget=20_000)["count"] == 1
    with pytest.raises(ToolError, match="budget"):
        _export(tool_app, doc, [PNG_64, PNG_64], byte_budget=20_000)
    assert len(fake_inkscape.calls) == 1


def test_tool_real_run_within_cap_and_budget(tool_app, fake_inkscape, doc) -> None:
    data = _export(
        tool_app,
        doc,
        [
            {"kind": "file", "format": "png", "width_px": 48},
            {"kind": "file", "format": "svg"},
            {"kind": "file", "format": "png", "object_id": "dot", "width_px": 24},
        ],
        byte_budget=1_000_000,
    )
    assert data["count"] == 3 and 0 < data["total_bytes"] <= 1_000_000
    root = Path(get_registry().get(doc).root)
    assert all(
        not a["path"].startswith("/") and (root / a["path"]).is_file() for a in data["artifacts"]
    )
    assert (root / data["artifacts"][0]["path"]).read_bytes()[:4] == PNG_MAGIC


def test_tool_file_targets_are_batched_around_other_targets(tool_app, fake_inkscape, doc) -> None:
    data = _export(
        tool_app,
        doc,
        [
            PNG_64,
            {"kind": "profile", "name": "icons", "sizes": [16]},
            {"kind": "file", "format": "svg"},
        ],
    )
    kinds = [a["kind"] for a in data["artifacts"]]
    # both file targets run together at the first file target, then the profile
    assert kinds == ["file", "file", "profile:icons"]


# --- real renderer ---------------------------------------------------------------------------


@pytest.mark.inkscape
def test_real_batch_run_exports_each_spec(doc) -> None:
    specs = [
        ExportSpec(format="png", width_px=48),
        ExportSpec(format="svg"),
        ExportSpec(format="png", object_id="dot", width_px=24),
    ]
    result = export_batch(doc, specs, dry_run=False)
    root = Path(get_registry().get(doc).root)
    assert result.item_count == 3 and (result.actual_total_bytes or 0) > 0
    assert (root / result.items[0].workspace_relative_path).read_bytes()[:4] == PNG_MAGIC
    assert all((root / i.workspace_relative_path).exists() for i in result.items)
