"""Shared mutating-edit pipeline tests.

Hermetic: `render_preview` is monkeypatched in the pipeline module so no test invokes Inkscape.
The fake writes a tiny PNG-ish file into the deterministic preview path and returns a
`RenderResult`-shaped object, mirroring how the real engine behaves (the pipeline then copies
that file to an operation-specific name).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from lxml import etree

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.edit import pipeline
from inkscape_mcp.edit.dom import EditError
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render.cli import RenderError, RenderResult
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.workspace import sandbox

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
    b'<rect id="r1" width="4" height="4"/></svg>'
)

#: A minimal PNG signature so the written file is plausibly a raster (content is irrelevant here).
PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-preview"


@pytest.fixture
def doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path, Path]:
    """Open a fixture SVG; return (doc_id, owning_root, original_source_path)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    src = ws / "logo.svg"
    src.write_bytes(SVG)
    entry = get_registry().open_document(str(src))
    return entry.doc_id, ws, src


def _install_fake_render(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace `render_preview` in the pipeline module with a hermetic fake.

    Writes a deterministic `artifacts/preview/preview-auto.png` (the real engine's no-width
    name) and returns a `RenderResult` whose `artifact_path` is workspace-relative to it.
    """

    def fake_render_preview(
        doc_id: str, width_px: int | None = None, settings: object | None = None
    ) -> RenderResult:
        entry = get_registry().get(doc_id)
        root = Path(entry.root)
        preview_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        out = preview_dir / "preview-auto.png"
        out.write_bytes(PNG_BYTES)
        # one-location contract: the real engine returns a path relative to the workspace
        # ROOT (carries the `.inkscape-mcp/documents/<doc_id>/...` base). Mirror that here so the
        # pipeline's `root / artifact_path` join resolves exactly as it does against real Inkscape.
        rel = out.relative_to(root).as_posix()
        return RenderResult(
            doc_id=doc_id,
            artifact_path=rel,
            workspace_relative_path=rel,
            format="png",
            width_px=10,
            height_px=10,
            duration_s=0.01,
        )

    monkeypatch.setattr(pipeline, "render_preview", fake_render_preview)


def _mutate_set_fill(tree: etree._ElementTree) -> str:
    """Set a fill on the fixture's `r1` rect (a real DOM change)."""
    root = tree.getroot()
    rect = root.find(".//{http://www.w3.org/2000/svg}rect")
    assert rect is not None
    rect.set("fill", "#ff0000")
    return "set fill of r1 to #ff0000"


def _mutate_noop(_tree: etree._ElementTree) -> str:
    """A mutation that touches nothing — the canonical bytes are unchanged (a genuine no-op)."""
    return "looked but changed nothing"


def _operation_record(root: Path, doc_id: str, operation_id: str) -> dict:
    op_file = sandbox.operations_dir(root, doc_id) / f"{operation_id}.json"
    return json.loads(op_file.read_text())


def test_happy_path_applies_links_snapshot_and_previews(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, src = doc
    _install_fake_render(monkeypatch)

    working = sandbox.working_copy(root, doc_id)

    result = pipeline.apply_edit(doc_id, "set_fill", {"fill": "#ff0000"}, _mutate_set_fill)

    # Result carries the change + summary.
    assert result.changed is True
    assert result.summary == "set fill of r1 to #ff0000"
    assert result.operation_id.startswith("op_")
    assert result.snapshot_id.startswith("snap_")

    # Operation Record on disk is `applied`, with snapshot + both previews linked.
    record = _operation_record(root, doc_id, result.operation_id)
    assert record["status"] == "applied"
    assert record["snapshot_id"] == result.snapshot_id
    assert set(record["previews"]) == {"before", "after"}

    # The op-specific before/after PNGs exist under artifacts/preview/.
    preview_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
    before_png = preview_dir / f"op-{result.operation_id}-before.png"
    after_png = preview_dir / f"op-{result.operation_id}-after.png"
    assert before_png.is_file()
    assert after_png.is_file()

    # The recorded preview paths are workspace-relative and resolve to those files.
    workspace_dir = Path(get_registry().get(doc_id).workspace_dir)
    assert (workspace_dir / record["previews"]["before"]) == before_png
    assert (workspace_dir / record["previews"]["after"]) == after_png
    assert result.preview_before == record["previews"]["before"]
    assert result.preview_after == record["previews"]["after"]

    # The working copy reflects the mutation; the ORIGINAL source file is byte-unchanged.
    assert b'fill="#ff0000"' in working.read_bytes()
    assert src.read_bytes() == SVG


@pytest.mark.inkscape
def test_apply_edit_against_real_render_preview(doc: tuple[str, Path, Path]) -> None:
    """End-to-end against the REAL `render_preview` engine — NO fake render.

    Guards the contract seam: `render_preview` returns an `artifact_path` relative to the
    workspace ROOT, and the pipeline must resolve it against `root` (not `workspace_dir`) when it
    copies the before/after frames. A regression here (the doubled `.inkscape-mcp/documents/<id>/`
    join) makes EVERY mutating edit raise `FileNotFoundError` on any host with Inkscape — but the
    hermetic fakes hide it, so this real-engine test is the only thing that catches it.
    """
    doc_id, root, _ = doc

    # No `_install_fake_render` — the real Inkscape-backed `render_preview` runs.
    result = pipeline.apply_edit(doc_id, "set_fill", {"fill": "#ff0000"}, _mutate_set_fill)

    assert result.changed is True
    assert result.operation_id.startswith("op_")
    # The op-specific before/after frames were copied successfully (no doubled-path FileNotFound).
    preview_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
    assert (preview_dir / f"op-{result.operation_id}-before.png").is_file()
    assert (preview_dir / f"op-{result.operation_id}-after.png").is_file()
    assert b'fill="#ff0000"' in sandbox.working_copy(root, doc_id).read_bytes()


def test_pre_mutation_snapshot_reverts_working_copy(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)

    working = sandbox.working_copy(root, doc_id)
    pre_edit_bytes = working.read_bytes()

    result = pipeline.apply_edit(doc_id, "set_fill", {"fill": "#ff0000"}, _mutate_set_fill)
    assert working.read_bytes() != pre_edit_bytes  # mutation landed

    # Restoring the linked pre-mutation snapshot returns the working copy to its pre-edit bytes.
    restore_snapshot(doc_id, result.snapshot_id)
    assert working.read_bytes() == pre_edit_bytes


def test_bad_input_discards_record_and_writes_nothing(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, src = doc
    _install_fake_render(monkeypatch)

    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()

    def _bad_mutate(_tree: etree._ElementTree) -> str:
        raise EditError("invalid edit input")

    with pytest.raises(EditError):
        pipeline.apply_edit(doc_id, "set_fill", {"fill": "bogus"}, _bad_mutate)

    # The record exists but is `discarded`; no snapshot was created.
    op_dir = sandbox.operations_dir(root, doc_id)
    records = [json.loads(p.read_text()) for p in op_dir.glob("op_*.json")]
    assert len(records) == 1
    assert records[0]["status"] == "discarded"
    assert records[0]["snapshot_id"] is None

    # No snapshot file / index was written.
    snap_dir = sandbox.snapshots_dir(root, doc_id)
    assert not sandbox.snapshots_index(root, doc_id).is_file()
    assert list(snap_dir.glob("*.svg")) == []

    # Working copy and original are untouched.
    assert working.read_bytes() == before
    assert src.read_bytes() == SVG


def test_noop_writes_nothing_and_reports_changed_false(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A mutation that leaves the canonical content byte-identical is a genuine no-op: the pipeline
    # must report changed=False and write NO snapshot and NO Operation Record (nothing to revert,
    # nothing to log). It is the single source of truth for `changed`.
    doc_id, root, src = doc
    _install_fake_render(monkeypatch)

    working = sandbox.working_copy(root, doc_id)
    before_working = working.read_bytes()

    result = pipeline.apply_edit(doc_id, "noop_tool", {}, _mutate_noop)

    assert result.changed is False
    # No op record / snapshot identity is handed back (nothing was created).
    assert result.operation_id == ""
    assert result.snapshot_id == ""
    assert result.preview_before is None
    assert result.preview_after is None
    assert result.summary is not None and "no change" in result.summary

    # Nothing was written: no Operation Record file, no snapshot file, no snapshot index.
    op_dir = sandbox.operations_dir(root, doc_id)
    assert list(op_dir.glob("op_*.json")) == []
    snap_dir = sandbox.snapshots_dir(root, doc_id)
    assert list(snap_dir.glob("*.svg")) == []
    assert not sandbox.snapshots_index(root, doc_id).is_file()

    # Working copy and original source are byte-unchanged.
    assert working.read_bytes() == before_working
    assert src.read_bytes() == SVG


def test_render_degradation_still_applies_with_none_previews(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc

    def failing_render_preview(
        doc_id: str, width_px: int | None = None, settings: object | None = None
    ) -> RenderResult:
        raise RenderError("render failed")

    monkeypatch.setattr(pipeline, "render_preview", failing_render_preview)

    working = sandbox.working_copy(root, doc_id)
    result = pipeline.apply_edit(doc_id, "set_fill", {"fill": "#ff0000"}, _mutate_set_fill)

    # The edit still applied: working copy changed, record `applied`, snapshot present.
    assert result.changed is True
    assert b'fill="#ff0000"' in working.read_bytes()

    record = _operation_record(root, doc_id, result.operation_id)
    assert record["status"] == "applied"
    assert record["snapshot_id"] == result.snapshot_id
    assert record["snapshot_id"].startswith("snap_")

    # Previews degraded to None and nothing was linked.
    assert result.preview_before is None
    assert result.preview_after is None
    assert record["previews"] == {}

    # No op-specific PNGs were written.
    preview_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
    assert not (preview_dir / f"op-{result.operation_id}-before.png").exists()
    assert not (preview_dir / f"op-{result.operation_id}-after.png").exists()
