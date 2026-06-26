"""Snapshot engine + tool tests (E1-07 / ADR-004)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.server import mcp
from inkscape_mcp.snapshots import SnapshotNotFound
from inkscape_mcp.tools.snapshots import create_snapshot, list_snapshots, restore_snapshot
from inkscape_mcp.workspace import sandbox

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'
SVG_MUTATED = b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"/>'


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


def test_create_snapshot_writes_file_and_index_original_untouched(
    doc: tuple[str, Path, Path],
) -> None:
    doc_id, root, src = doc

    info = create_snapshot(doc_id, label="first")

    # A file landed under the per-document snapshots dir with the indexed basename.
    snap_dir = sandbox.snapshots_dir(root, doc_id)
    snap_file = snap_dir / info.file
    assert snap_file.is_file()
    assert snap_file.parent == snap_dir  # basename, no traversal
    assert snap_file.read_bytes() == SVG

    # index.json gained the entry.
    index = json.loads(sandbox.snapshots_index(root, doc_id).read_text())
    assert len(index["snapshots"]) == 1
    assert index["snapshots"][0]["snapshot_id"] == info.snapshot_id
    assert index["snapshots"][0]["seq"] == 1
    assert index["snapshots"][0]["label"] == "first"

    # SnapshotInfo metadata.
    assert info.snapshot_id.startswith("snap_")
    assert info.seq == 1
    assert info.size_bytes == len(SVG)
    assert info.operation_id is None

    # The ORIGINAL source file is byte-unchanged.
    assert src.read_bytes() == SVG


def test_list_snapshots_empty_then_ordered(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, _ = doc

    assert list_snapshots(doc_id).snapshots == []

    a = create_snapshot(doc_id)
    b = create_snapshot(doc_id)

    listed = list_snapshots(doc_id).snapshots
    assert [s.snapshot_id for s in listed] == [a.snapshot_id, b.snapshot_id]
    assert [s.seq for s in listed] == [1, 2]


def test_restore_reverts_working_copy_records_operation(
    doc: tuple[str, Path, Path],
) -> None:
    doc_id, root, src = doc

    # Snapshot the as-opened state.
    first = create_snapshot(doc_id, label="baseline")

    # Mutate the working copy directly, then snapshot the mutated state.
    working = sandbox.working_copy(root, doc_id)
    working.write_bytes(SVG_MUTATED)
    create_snapshot(doc_id, label="mutated")
    assert working.read_bytes() == SVG_MUTATED

    # Restore back to the FIRST snapshot.
    result = restore_snapshot(doc_id, first.snapshot_id)

    # Working copy bytes equal the first snapshot's content.
    assert working.read_bytes() == SVG
    assert result.restored_from == first.snapshot_id

    # The original is still untouched.
    assert src.read_bytes() == SVG

    # An Operation Record file exists for the restore and references a pre-restore snapshot.
    op_file = sandbox.operations_dir(root, doc_id) / f"{result.operation_id}.json"
    assert op_file.is_file()
    record = json.loads(op_file.read_text())
    assert record["tool"] == "restore_snapshot"
    assert record["status"] == "applied"
    assert record["snapshot_id"] == result.pre_restore_snapshot_id
    assert result.pre_restore_snapshot_id.startswith("snap_")

    # The pre-restore snapshot is itself in the index (reversible undo).
    snap_ids = {s.snapshot_id for s in list_snapshots(doc_id).snapshots}
    assert result.pre_restore_snapshot_id in snap_ids


def test_restore_returns_sha256_and_size_of_restored_content(
    doc: tuple[str, Path, Path],
) -> None:
    # E11-10(d) / S11: restore returns restored_sha256 + restored_size_bytes so a caller can
    # assert recovery WITHOUT reading the working copy off disk.
    doc_id, root, _ = doc

    first = create_snapshot(doc_id, label="baseline")

    # Mutate the working copy, snapshot it, then restore back to the baseline.
    working = sandbox.working_copy(root, doc_id)
    working.write_bytes(SVG_MUTATED)
    create_snapshot(doc_id, label="mutated")

    result = restore_snapshot(doc_id, first.snapshot_id)

    # The returned digest + size describe the RESTORED content (the original SVG bytes), and they
    # match an independent hash of the on-disk working copy — proving the assertion is sound.
    assert result.restored_sha256 == hashlib.sha256(SVG).hexdigest()
    assert result.restored_size_bytes == len(SVG)
    assert result.restored_sha256 == hashlib.sha256(working.read_bytes()).hexdigest()
    assert result.restored_size_bytes == working.stat().st_size

    # After mutating-and-restoring, the digest reflects the restored bytes, NOT the mutated ones.
    assert result.restored_sha256 != hashlib.sha256(SVG_MUTATED).hexdigest()


def test_restore_unknown_id_raises_and_writes_nothing_outside(
    doc: tuple[str, Path, Path],
) -> None:
    doc_id, root, src = doc
    create_snapshot(doc_id)

    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()

    # Crafted traversal id: rejected by shape + index check, nothing restored.
    with pytest.raises((ToolError, SnapshotNotFound)):
        restore_snapshot(doc_id, "../../etc/passwd")

    # A well-formed but absent id is likewise rejected.
    with pytest.raises((ToolError, SnapshotNotFound)):
        restore_snapshot(doc_id, "snap_deadbeef")

    # Working copy and original are untouched; no escape write occurred.
    assert working.read_bytes() == before
    assert src.read_bytes() == SVG
    assert not (root / "etc").exists()


def test_unknown_doc_id_maps_to_toolerror(doc: tuple[str, Path, Path]) -> None:
    with pytest.raises(ToolError) as exc:
        create_snapshot("d_nope")
    assert "not found" in str(exc.value)
    with pytest.raises(ToolError):
        list_snapshots("d_nope")
    with pytest.raises(ToolError):
        restore_snapshot("d_nope", "snap_deadbeef")


def test_tools_registered_on_mcp(doc: tuple[str, Path, Path]) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "create_snapshot" in names
    assert "list_snapshots" in names
    assert "restore_snapshot" in names


def test_create_snapshot_rejects_overlong_label(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, _ = doc
    with pytest.raises(ToolError):
        create_snapshot(doc_id, label="x" * 257)


def test_engine_create_snapshot_rejects_bad_operation_id(doc: tuple[str, Path, Path]) -> None:
    # The engine puts operation_id into the snapshot filename, so a non-`op_<hex>` token
    # (e.g. a traversal string) must be refused before it touches a path.
    from inkscape_mcp.snapshots import create_snapshot as engine_create_snapshot

    doc_id, root, _ = doc
    with pytest.raises(SnapshotNotFound):
        engine_create_snapshot(doc_id, operation_id="../../etc/passwd")
    assert not (root / "etc").exists()
