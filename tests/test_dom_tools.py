"""`delete_object` tool tests (E16-08 / ADR-002 / ADR-004 / ADR-005).

HIGH-risk, reversible DOM delete. Hermetic: `render_preview` is monkeypatched in the pipeline
module so no test invokes Inkscape (mirrors `test_transform_tools.py`). Covers: removal + affected
ids, the pre-op snapshot + Operation Record, reversibility via `restore_snapshot`, the no-match
no-op (changed=False, no snapshot written), and the HIGH-risk approval gate.
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
from inkscape_mcp.tools.dom import delete_object
from inkscape_mcp.workspace import sandbox

SVG_NS = "http://www.w3.org/2000/svg"

#: Three addressable rects so a delete can remove a subset and leave a remainder.
SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
    b'<rect id="keep" x="0" y="0" width="10" height="10"/>'
    b'<rect id="seed1" x="20" y="0" width="10" height="10"/>'
    b'<rect id="seed2" x="40" y="0" width="10" height="10"/>'
    b"</svg>"
)

#: A valid (non-empty, non-whitespace) approval token for the HIGH-risk gate.
TOKEN = "approve-delete-1"

PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-preview"


def _make_doc(ws: Path, data: bytes) -> tuple[str, Path]:
    reset_registry()
    src = ws / "logo.svg"
    src.write_bytes(data)
    entry = get_registry().open_document(str(src))
    return entry.doc_id, ws


@pytest.fixture
def doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    return _make_doc(ws, SVG)


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


def _working_tree(root: Path, doc_id: str) -> etree._Element:
    return etree.parse(str(sandbox.working_copy(root, doc_id))).getroot()


def _ids(root: Path, doc_id: str) -> set[str]:
    tree = _working_tree(root, doc_id)
    return {e.get("id") for e in tree.iter() if isinstance(e.tag, str) and e.get("id")}


def _operation_record(root: Path, doc_id: str, operation_id: str) -> dict:
    op_file = sandbox.operations_dir(root, doc_id) / f"{operation_id}.json"
    return json.loads(op_file.read_text())


def _snapshot_count(root: Path, doc_id: str) -> int:
    snap_dir = sandbox.snapshots_dir(root, doc_id)
    return len(list(snap_dir.glob("*.svg"))) if snap_dir.exists() else 0


def _op_count(root: Path, doc_id: str) -> int:
    op_dir = sandbox.operations_dir(root, doc_id)
    return len(list(op_dir.glob("op_*.json"))) if op_dir.exists() else 0


# --- removal + affected ids --------------------------------------------------


def test_delete_object_removes_elements_and_returns_affected_ids(doc: tuple[str, Path]) -> None:
    doc_id, root = doc

    result = delete_object(doc_id, ["seed1", "seed2"], approval_token=TOKEN)

    assert result.changed is True
    assert result.affected_ids == ["seed1", "seed2"]
    # The two seed rects are gone; the kept one remains.
    assert _ids(root, doc_id) == {"keep"}


def test_delete_object_writes_pre_op_snapshot_and_record(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    snaps_before = _snapshot_count(root, doc_id)
    ops_before = _op_count(root, doc_id)

    result = delete_object(doc_id, ["seed1"], approval_token=TOKEN)

    assert result.operation_id.startswith("op_")
    assert result.snapshot_id.startswith("snap_")
    assert _snapshot_count(root, doc_id) == snaps_before + 1
    assert _op_count(root, doc_id) == ops_before + 1
    record = _operation_record(root, doc_id, result.operation_id)
    assert record["status"] == "applied"
    # HIGH risk is recorded on the operation.
    assert record["risk_class"] == "high"


def test_delete_object_reversible_via_restore_snapshot(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    working = sandbox.working_copy(root, doc_id)
    pre = working.read_bytes()

    result = delete_object(doc_id, ["seed1", "seed2"], approval_token=TOKEN)
    assert working.read_bytes() != pre
    assert _ids(root, doc_id) == {"keep"}

    restore_snapshot(doc_id, result.snapshot_id)
    # The deleted geometry is recovered byte-for-byte.
    assert working.read_bytes() == pre
    assert _ids(root, doc_id) == {"keep", "seed1", "seed2"}


# --- no-op hygiene (E10-05 / E11-13) ----------------------------------------


def test_delete_object_no_match_reports_changed_false_no_snapshot(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()
    snaps_before = _snapshot_count(root, doc_id)
    ops_before = _op_count(root, doc_id)

    result = delete_object(doc_id, ["nope", "also-missing"], approval_token=TOKEN)

    assert result.changed is False
    assert result.operation_id == ""
    assert result.snapshot_id == ""
    assert result.affected_ids == []
    # Nothing was written: working copy byte-identical, no new snapshot / Operation Record.
    assert working.read_bytes() == before
    assert _snapshot_count(root, doc_id) == snaps_before
    assert _op_count(root, doc_id) == ops_before


def test_delete_object_empty_list_rejected(doc: tuple[str, Path]) -> None:
    doc_id, _ = doc
    with pytest.raises(ToolError):
        delete_object(doc_id, [], approval_token=TOKEN)


def test_delete_object_partial_match_only_removes_present(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    # One present id + one absent id: only the present one is removed/reported.
    result = delete_object(doc_id, ["seed1", "ghost"], approval_token=TOKEN)
    assert result.changed is True
    assert result.affected_ids == ["seed1"]
    assert _ids(root, doc_id) == {"keep", "seed2"}


# --- HIGH-risk approval gate ------------------------------------------------


def test_delete_object_without_token_refused_no_change(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()
    snaps_before = _snapshot_count(root, doc_id)

    with pytest.raises(ToolError):
        delete_object(doc_id, ["seed1"], approval_token=None)

    # Refused before any write: working copy unchanged, no snapshot.
    assert working.read_bytes() == before
    assert _snapshot_count(root, doc_id) == snaps_before


def test_delete_object_whitespace_token_refused(doc: tuple[str, Path]) -> None:
    doc_id, _ = doc
    with pytest.raises(ToolError):
        delete_object(doc_id, ["seed1"], approval_token="   ")


# --- error mapping ----------------------------------------------------------


def test_delete_object_unknown_doc_maps_to_toolerror(doc: tuple[str, Path]) -> None:
    with pytest.raises(ToolError) as exc:
        delete_object("d_nope", ["seed1"], approval_token=TOKEN)
    assert "document id not found" in str(exc.value)


# --- registration -----------------------------------------------------------


def test_delete_object_registered_on_mcp(doc: tuple[str, Path]) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "delete_object" in names
