"""Batch exports: `render.batch.export_batch` (engine) and its use by `export` file targets.

Several `{"kind": "file"}` targets of one document run as ONE bounded batch: item cap, a
conservative byte projection checked against `byte_budget` before anything is written, dry run.
The `inkscape` binary is faked (see `test_export_files.FakeInkscape`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_ARTIFACT_MAX_BYTES_PER_DOC, get_settings
from inkscape_mcp.document.inspect import DocumentNotFound
from inkscape_mcp.registry import get_registry
from inkscape_mcp.render import cli as render_cli
from inkscape_mcp.render.batch import MAX_BATCH_ITEMS, BatchError, ExportSpec, export_batch
from inkscape_mcp.render.cli import RenderError
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
    assert all(i.workspace_relative_path is None for i in result.items)
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
    assert "upper bound" in data["projected_note"]
    assert _written(doc) == [] and fake_inkscape.calls == []


def test_tool_dry_run_without_a_png_has_no_projected_note(tool_app, fake_inkscape, doc) -> None:
    data = _export(tool_app, doc, [{"kind": "save_as", "dest_path": "x.svg"}], dry_run=True)
    assert data["projected_bytes"] == 0 and "projected_note" not in data


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


def test_tool_budget_covers_all_file_targets_together(tool_app, fake_inkscape, doc) -> None:
    # one 64 px PNG projects to 13 056 B: it fits 20 000, two together do not
    assert _export(tool_app, doc, [PNG_64], byte_budget=20_000)["count"] == 1
    with pytest.raises(ToolError, match="budget"):
        _export(tool_app, doc, [PNG_64, PNG_64], byte_budget=20_000)
    assert len(fake_inkscape.calls) == 1


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


# --- partial failure: no half result is left behind -----------------------------------------


def _all_exports(tool_ws: Path) -> list[Path]:
    return [p for p in tool_ws.rglob("*") if p.is_file() and p.suffix in {".png", ".pdf"}]


def test_engine_failure_mid_batch_removes_the_items_already_written(
    fake_inkscape, doc, tool_ws
) -> None:
    fake_inkscape.fail_on_call = 2
    specs = [ExportSpec(format="png", width_px=16), ExportSpec(format="pdf")]
    with pytest.raises(RenderError, match="render failed"):
        export_batch(doc, specs, dry_run=False)
    assert len(fake_inkscape.calls) == 2 and _written(doc) == []


def test_engine_budget_overrun_mid_batch_removes_every_item(fake_inkscape, doc) -> None:
    # An SVG projects as max(working size, 4096) but this one writes far more than budgeted.
    fake_inkscape.svg_body = b" " * 10_000
    specs = [ExportSpec(format="png", width_px=16), ExportSpec(format="svg")]
    with pytest.raises(BatchError, match="exceeded the byte budget mid-run"):
        export_batch(doc, specs, dry_run=False, byte_budget=6000)
    assert _written(doc) == []


@pytest.mark.parametrize(
    ("targets", "failing"),
    [
        # both file targets run as one batch; the error must still name the second one
        ([PNG_64, {"kind": "file", "format": "pdf"}], r"targets\[1\] \(file\)"),
        (
            [PNG_64, {"kind": "profile", "name": "icons", "sizes": [16]}],
            r"targets\[1\] \(profile\)",
        ),
    ],
)
def test_tool_failing_second_target_names_it_and_keeps_no_artifact(
    tool_app, fake_inkscape, doc, tool_ws, targets, failing
) -> None:
    fake_inkscape.fail_on_call = 2
    # 2 real deletions: the finished PNG + the failing target's claimed (half-written) output.
    with pytest.raises(
        ToolError, match=rf"^{doc} {failing}: render/export failed.*2 partial"
    ) as exc:
        _export(tool_app, doc, targets)
    assert "removed" in str(exc.value)
    assert _all_exports(tool_ws) == []


def test_tool_failed_profile_leaves_no_save_as(
    tool_app, fake_inkscape, doc, tool_ws, monkeypatch
) -> None:
    targets = [
        {"kind": "save_as", "dest_path": "deliver/logo.svg"},
        {"kind": "file", "format": "png", "width_px": 32},
        {"kind": "profile", "name": "icons"},
    ]

    def boom(*_a, **_k):
        raise RuntimeError("render failed")

    with monkeypatch.context() as m:
        m.setattr("inkscape_mcp.tools.export._profile", boom)
        with pytest.raises(ToolError, match="partial artifact"):
            _export(tool_app, doc, targets)
    assert not (tool_ws / "deliver" / "logo.svg").exists()
    assert not list((tool_ws / "deliver").glob("*"))
    assert _export(tool_app, doc, targets)["saved"]["path"] == "deliver/logo.svg"


def test_tool_save_as_onto_existing_removes_earlier_files(
    tool_app, fake_inkscape, doc, tool_ws
) -> None:
    (tool_ws / "deliver").mkdir()
    existing = tool_ws / "deliver" / "keep.svg"
    existing.write_bytes(b"<svg/>")
    with pytest.raises(ToolError, match="already exists"):
        _export(
            tool_app,
            doc,
            [
                {"kind": "file", "format": "png", "width_px": 32},
                {"kind": "save_as", "dest_path": "deliver/keep.svg"},
            ],
        )
    assert [p.name for p in (tool_ws / "deliver").iterdir()] == ["keep.svg"]
    assert existing.read_bytes() == b"<svg/>"


def test_tool_failing_second_document_removes_the_first_documents_files(
    tool_app, fake_inkscape, tool_ws
) -> None:
    a, b = open_doc(tool_ws, "a.svg", ART), open_doc(tool_ws, "b.svg", ART)
    fake_inkscape.fail_on_call = 2
    # 2 real deletions: a's finished PNG + b's claimed (half-written) output.
    with pytest.raises(ToolError, match=rf"^{b} targets\[0\] \(file\): .*2 partial"):
        _export(tool_app, [a, b], [PNG_64])
    assert _all_exports(tool_ws) == []


# --- output ownership / budget backstops (security review) -----------------------------------


def test_identical_targets_in_one_call_get_distinct_files(
    tool_app, fake_inkscape, doc, tool_ws
) -> None:
    out = _export(tool_app, doc, [PNG_64, PNG_64])
    paths = [a["path"] for a in out["artifacts"]]
    assert len(set(paths)) == 2 and all((tool_ws / p).is_file() for p in paths)


def test_existing_output_is_never_overwritten_or_deleted(
    fake_inkscape, doc, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(get_registry().get(doc).root)
    exports = sandbox.exports_dir(root, doc)
    exports.mkdir(parents=True, exist_ok=True)
    earlier = exports / "taken.png"
    earlier.write_bytes(b"earlier artifact")
    monkeypatch.setattr(render_cli, "_output_name", lambda *a, **k: "taken.png")
    with pytest.raises(RenderError, match="already exists"):
        export_batch(doc, [ExportSpec(format="png", width_px=16)], dry_run=False)
    assert earlier.read_bytes() == b"earlier artifact" and fake_inkscape.calls == []


def test_cancel_during_inkscape_removes_the_half_written_output(fake_inkscape, doc) -> None:
    class Cancelled(BaseException):
        pass

    fake_inkscape.error = Cancelled()
    with pytest.raises(Cancelled):
        export_batch(doc, [ExportSpec(format="png", width_px=16)], dry_run=False)
    assert _written(doc) == []


def test_safe_unlink_refuses_a_parent_swapped_for_a_symlink(tmp_path: Path) -> None:
    real, other = tmp_path / "real", tmp_path / "other"
    real.mkdir(), other.mkdir()
    tracked = render_cli._claim_output(real / "a.png")
    victim = other / "a.png"
    victim.write_bytes(b"not ours")
    tracked.unlink()
    real.rmdir()
    real.symlink_to(other)
    assert render_cli._safe_unlink(tracked) is False and victim.exists()


@pytest.mark.parametrize("link", [False, True])
def test_out_dir_into_existing_state_dir_is_refused(fake_inkscape, doc, tool_ws, link) -> None:
    out_dir = ".inkscape-mcp/documents"
    if link:
        (tool_ws / "state-link").symlink_to(tool_ws / ".inkscape-mcp")
        out_dir = "state-link/documents"
    with pytest.raises(SandboxViolation, match=r"managed \.inkscape-mcp"):
        export_batch(doc, [ExportSpec(format="png", width_px=16)], out_dir=out_dir)
    assert fake_inkscape.calls == []


def test_profile_output_over_budget_mid_run_aborts_and_discards(
    tool_app, fake_inkscape, doc, tool_ws
) -> None:
    targets = [PNG_64, {"kind": "profile", "name": "web", "width_px": 32}]
    projected = _export(tool_app, doc, targets, dry_run=True)["projected_bytes"]
    fake_inkscape.svg_body = b" " * (projected + 1)  # the vector projection under-estimates
    with pytest.raises(ToolError, match=r"targets\[1\] \(profile\): .*mid-run"):
        _export(tool_app, doc, targets, byte_budget=projected)
    assert _all_exports(tool_ws) == [] and not any(p.suffix == ".svg" for p in _written(doc))


@pytest.mark.xfail(
    strict=True,
    reason="artifact_max_bytes_per_doc is enforced per export call only (batch budget clamp); "
    "docs/design/workspace-model.md §6 promises a cumulative per-document artifact cap",
)
def test_per_document_artifact_cap_is_cumulative(fake_inkscape, doc, monkeypatch) -> None:
    # one 64 px PNG projects to 13 056 bytes: each call fits alone, two together do not
    monkeypatch.setenv(ENV_ARTIFACT_MAX_BYTES_PER_DOC, "20000")
    get_settings.cache_clear()
    export_batch(doc, [ExportSpec(format="png", width_px=64)], dry_run=False)
    with pytest.raises(BatchError, match="byte budget"):
        export_batch(doc, [ExportSpec(format="png", width_px=64)], dry_run=False)


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
