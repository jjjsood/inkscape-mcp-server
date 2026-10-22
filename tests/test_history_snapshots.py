"""Snapshot engine (ADR-004) and the `history` tool: list / create / restore / reload.

Engine tests pin the on-disk contract (index, files, operation record, sha256 of restored content,
id shape guards); tool tests pin how `history` surfaces it. Reload is covered against external
source changes, removed sources, created (source-less) documents and reversibility.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.registry import get_registry
from inkscape_mcp.snapshots import (
    SnapshotNotFound,
    create_snapshot,
    list_snapshots,
    restore_snapshot,
)
from inkscape_mcp.workspace import sandbox
from tests.tool_support import call, write_svg

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'
MUTATED = b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"/>'

V1 = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">
  <rect id="r1" x="1" y="1" width="2" height="2"/>
</svg>
"""
V2 = V1.replace(b"</svg>", b'  <circle id="c2" cx="5" cy="5" r="3"/>\n</svg>')


@pytest.fixture
def doc(tool_ws) -> tuple[str, Path, Path]:
    """(doc_id, owning root, source file) for a freshly opened SVG."""
    src = write_svg(tool_ws, "logo.svg", SVG)
    return get_registry().open_document(str(src)).doc_id, tool_ws, src


def _hist(app, doc_id, action, **kw):
    return call(app, "history", {"doc_id": doc_id, "action": action, **kw}).data


# --- snapshot engine ----------------------------------------------------------------------------


def test_create_writes_the_file_and_index_and_leaves_the_source(doc) -> None:
    doc_id, root, src = doc
    info = create_snapshot(doc_id, label="first")

    snap_dir = sandbox.snapshots_dir(root, doc_id)
    snap_file = snap_dir / info.file
    assert snap_file.parent == snap_dir and snap_file.read_bytes() == SVG  # basename, no traversal
    (entry,) = json.loads(sandbox.snapshots_index(root, doc_id).read_text())["snapshots"]
    assert (entry["snapshot_id"], entry["seq"], entry["label"]) == (info.snapshot_id, 1, "first")
    assert info.snapshot_id.startswith("snap_") and info.size_bytes == len(SVG)
    assert info.operation_id is None
    assert src.read_bytes() == SVG


def test_list_is_empty_then_ordered_by_seq(doc) -> None:
    doc_id, _, _ = doc
    assert list_snapshots(doc_id) == []
    a, b = create_snapshot(doc_id), create_snapshot(doc_id)
    listed = list_snapshots(doc_id)
    assert [s.snapshot_id for s in listed] == [a.snapshot_id, b.snapshot_id]
    assert [s.seq for s in listed] == [1, 2]


def test_restore_reverts_the_working_copy_and_records_the_operation(doc) -> None:
    doc_id, root, src = doc
    first = create_snapshot(doc_id, label="baseline")
    working = sandbox.working_copy(root, doc_id)
    working.write_bytes(MUTATED)
    create_snapshot(doc_id, label="mutated")

    result = restore_snapshot(doc_id, first.snapshot_id)

    assert working.read_bytes() == SVG and result.restored_from == first.snapshot_id
    assert src.read_bytes() == SVG
    record = json.loads(
        (sandbox.operations_dir(root, doc_id) / f"{result.operation_id}.json").read_text()
    )
    assert record["tool"] == "restore_snapshot" and record["status"] == "applied"
    assert record["snapshot_id"] == result.pre_restore_snapshot_id
    # the pre-restore snapshot is indexed, so the restore itself is reversible
    assert result.pre_restore_snapshot_id in {s.snapshot_id for s in list_snapshots(doc_id)}


def test_restore_reports_sha256_and_size_of_the_restored_bytes(doc) -> None:
    doc_id, root, _ = doc
    first = create_snapshot(doc_id)
    working = sandbox.working_copy(root, doc_id)
    working.write_bytes(MUTATED)

    result = restore_snapshot(doc_id, first.snapshot_id)

    assert result.restored_sha256 == hashlib.sha256(SVG).hexdigest()
    assert result.restored_sha256 == hashlib.sha256(working.read_bytes()).hexdigest()
    assert result.restored_size_bytes == len(SVG) == working.stat().st_size


@pytest.mark.parametrize("bad_id", ["../../etc/passwd", "snap_deadbeef"])
def test_restore_rejects_crafted_or_absent_ids_and_writes_nothing(doc, bad_id: str) -> None:
    doc_id, root, src = doc
    create_snapshot(doc_id)
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()

    with pytest.raises(SnapshotNotFound):
        restore_snapshot(doc_id, bad_id)

    assert working.read_bytes() == before and src.read_bytes() == SVG
    assert not (root / "etc").exists()


def test_engine_rejects_a_non_op_token_as_operation_id(doc) -> None:
    """The operation id becomes part of the snapshot filename, so it is shape-checked first."""
    doc_id, root, _ = doc
    with pytest.raises(SnapshotNotFound):
        create_snapshot(doc_id, operation_id="../../etc/passwd")
    assert not (root / "etc").exists()


# --- `history` tool over the same engine --------------------------------------------------------


def test_tool_list_orders_snapshots_and_restore_matches_engine(tool_app, doc) -> None:
    doc_id, root, _ = doc
    empty = _hist(tool_app, doc_id, "list")
    assert empty["count"] == 0 and "snapshots" not in empty
    made = [_hist(tool_app, doc_id, "create", label=f"s{i}") for i in range(2)]
    listed = _hist(tool_app, doc_id, "list")["snapshots"]
    assert [s["id"] for s in listed] == [m["snapshot_id"] for m in made]
    assert [s["seq"] for s in listed] == [1, 2]
    assert listed[0]["bytes"] == len(SVG)

    sandbox.working_copy(root, doc_id).write_bytes(MUTATED)
    res = _hist(tool_app, doc_id, "restore", snapshot_id=made[0]["snapshot_id"])
    assert sandbox.working_copy(root, doc_id).read_bytes() == SVG
    assert res["restored_from"] == made[0]["snapshot_id"]
    assert res["pre_restore_snapshot_id"].startswith("snap_") and res["operation_id"].startswith(
        "op_"
    )
    assert len(_hist(tool_app, doc_id, "list")["snapshots"]) == 3


def test_tool_restore_of_a_bad_id_is_actionable_and_harmless(tool_app, doc) -> None:
    # the engine test covers absent ids too; the tool must surface the crafted one as a hint
    doc_id, root, _ = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    with pytest.raises(ToolError, match=r"snapshot not found; call history\(action='list'\)"):
        _hist(tool_app, doc_id, "restore", snapshot_id="../../etc/passwd")
    assert sandbox.working_copy(root, doc_id).read_bytes() == before


def test_tool_create_label_is_listed_and_length_limited(tool_app, doc) -> None:
    doc_id, _, _ = doc
    assert _hist(tool_app, doc_id, "create", label="before-logo")["snapshot_id"]
    assert _hist(tool_app, doc_id, "list")["snapshots"][0]["label"] == "before-logo"
    with pytest.raises(ToolError, match="label too long"):
        _hist(tool_app, doc_id, "create", label="x" * 300)


def test_tool_arguments_must_match_the_action(tool_app, doc) -> None:
    doc_id, _, _ = doc
    with pytest.raises(ToolError, match="requires snapshot_id"):
        _hist(tool_app, doc_id, "restore")
    with pytest.raises(ToolError, match="only used with action='restore'"):
        _hist(tool_app, doc_id, "list", snapshot_id="x")
    with pytest.raises(ToolError, match="only used with action='create'"):
        _hist(tool_app, doc_id, "list", label="x")


@pytest.mark.parametrize("action", ["list", "create", "restore", "prune", "reload"])
def test_every_action_refuses_an_unknown_document(tool_app, tool_ws, action: str) -> None:
    args = {"snapshot_id": "snap_deadbeef"} if action == "restore" else {}
    with pytest.raises(ToolError, match="not found"):
        _hist(tool_app, "d_nope", action, **args)


# --- reload -------------------------------------------------------------------------------------


def _open(app, ws, data=V1) -> tuple[str, Path]:
    src = write_svg(ws, "a.svg", data)
    return call(app, "open", {"path": "a.svg"}).data["doc_id"], src


def _working(doc_id: str) -> Path:
    return Path(get_registry().get(doc_id).working_path)


def test_reload_pulls_the_updated_source_under_the_same_id(tool_app, tool_ws) -> None:
    doc_id, src = _open(tool_app, tool_ws)
    src.write_bytes(V2)  # external edit
    res = _hist(tool_app, doc_id, "reload")
    assert res["doc_id"] == doc_id and res["summary"]["objects"] == 2
    assert 'id="c2"' in _working(doc_id).read_text(encoding="utf-8")


def test_reload_snapshots_first_and_is_reversible(tool_app, tool_ws) -> None:
    doc_id, src = _open(tool_app, tool_ws)
    before = _working(doc_id).read_bytes()
    src.write_bytes(V2)
    res = _hist(tool_app, doc_id, "reload")
    snaps = list_snapshots(doc_id)
    assert [s.snapshot_id for s in snaps] == [res["pre_reload_snapshot_id"]]
    assert snaps[0].label == "pre-reload"
    assert _working(doc_id).read_bytes() != before
    _hist(tool_app, doc_id, "restore", snapshot_id=res["pre_reload_snapshot_id"])
    assert _working(doc_id).read_bytes() == before


def test_reload_discards_working_edits(tool_app, tool_ws) -> None:
    doc_id, _ = _open(tool_app, tool_ws)
    _working(doc_id).write_bytes(V2)
    _hist(tool_app, doc_id, "reload")
    assert 'id="c2"' not in _working(doc_id).read_text(encoding="utf-8")


def test_reload_of_a_created_document_restores_the_blank_seed(tool_app, tool_ws) -> None:
    doc_id = call(tool_app, "open", {"canvas": {"width": 20, "height": 20}}).data["doc_id"]
    seed = _working(doc_id).read_bytes()
    _working(doc_id).write_bytes(SVG)
    assert _hist(tool_app, doc_id, "reload")["doc_id"] == doc_id
    assert _working(doc_id).read_bytes() == seed


def test_reload_falls_back_to_the_baseline_when_the_source_is_gone(tool_app, tool_ws) -> None:
    """A vanished source cannot redirect the reload; it restores the in-sandbox original.svg."""
    doc_id, src = _open(tool_app, tool_ws)
    src.unlink()
    res = _hist(tool_app, doc_id, "reload")
    assert res["doc_id"] == doc_id
    assert 'id="r1"' in _working(doc_id).read_text(encoding="utf-8")


def test_restoring_the_pre_restore_snapshot_undoes_the_restore(tool_app, tool_ws) -> None:
    """Undo of undo: restore to the pre-edit snapshot, then restore its pre_restore snapshot."""
    doc_id, _ = _open(tool_app, tool_ws)
    edit = call(
        tool_app,
        "edit",
        {"doc_id": doc_id, "ops": [{"op": "fill", "ids": ["r1"], "color": "#00ff00"}]},
    ).data
    edited = _working(doc_id).read_bytes()

    undo = _hist(tool_app, doc_id, "restore", snapshot_id=edit["snapshot_id"])
    assert _working(doc_id).read_bytes() != edited
    _hist(tool_app, doc_id, "restore", snapshot_id=undo["pre_restore_snapshot_id"])
    assert _working(doc_id).read_bytes() == edited


def test_edit_snapshot_is_labelled_with_what_it_precedes(tool_app, tool_ws) -> None:
    doc_id, _ = _open(tool_app, tool_ws)
    edit = call(
        tool_app,
        "edit",
        {
            "doc_id": doc_id,
            "ops": [{"op": "fill", "ids": ["r1"], "color": "#00ff00"}],
            "detail": "summary",
        },
    ).data
    assert "BEFORE this edit" in edit["snapshot_note"]
    listed = _hist(tool_app, doc_id, "list")
    assert "BEFORE the edit" in listed["note"]
    entry = next(s for s in listed["snapshots"] if s["id"] == edit["snapshot_id"])
    assert entry["label"].startswith("before: fill")
    assert entry["restoring"] == f"returns to the state {entry['label']}"
    assert len(entry["label"]) <= 120
