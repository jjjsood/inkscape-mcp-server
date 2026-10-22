"""Snapshot and live-frame retention: the pruning policy (workspace-model section 6) and
`history(prune)`, plus the rule that retention is an EXPLICIT pass only (no edit or render runs it).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import (
    ENV_SNAPSHOT_KEEP_DAYS,
    ENV_SNAPSHOT_KEEP_N,
    Settings,
    get_settings,
)
from inkscape_mcp.live import session as session_mod
from inkscape_mcp.live.records import LiveOperationRecord, _persist
from inkscape_mcp.live.render import render_live_view
from inkscape_mcp.live.session import LiveSessionManager
from inkscape_mcp.operations import OperationStatus, new_operation, update_operation
from inkscape_mcp.registry import get_registry
from inkscape_mcp.retention import (
    prune_document,
    prune_live_frames_at,
    prune_snapshots_at,
    sweep_all_roots,
)
from inkscape_mcp.snapshots import create_snapshot, list_snapshots
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.risk import RiskClass
from tests.conftest import FakeTransport
from tests.tool_support import call, write_svg

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'
SNAP_BYTES = len(SVG)
FRAME_BYTES = 1024


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Tests set retention env vars; don't leak the cached Settings into later tests."""
    yield
    get_settings.cache_clear()


@pytest.fixture
def doc(tool_ws) -> tuple[str, Path]:
    return get_registry().open_document(str(write_svg(tool_ws, "logo.svg", SVG))).doc_id, tool_ws


def _settings(root: Path, **overrides: int) -> Settings:
    return Settings(workspace_roots=[root], **overrides)


def _create_n(doc_id: str, n: int) -> list[str]:
    """`n` snapshots, oldest first."""
    return [create_snapshot(doc_id).snapshot_id for _ in range(n)]


def _remaining(doc_id: str) -> list[str]:
    return [s.snapshot_id for s in list_snapshots(doc_id)]


# --- snapshot policy ----------------------------------------------------------------------------


def test_keep_n_prunes_the_oldest(doc) -> None:
    doc_id, root = doc
    ids = _create_n(doc_id, 5)
    result = prune_snapshots_at(
        root, doc_id, _settings(root, snapshot_keep_n=2, snapshot_keep_days=0)
    )

    assert set(result.pruned_snapshot_ids) == set(ids[:3])
    assert result.retained_count == 2 and result.freed_bytes == 3 * SNAP_BYTES
    assert _remaining(doc_id) == ids[3:]
    assert len(list(sandbox.snapshots_dir(root, doc_id).glob("*.svg"))) == 2


def test_keep_days_is_unioned_with_the_count(doc) -> None:
    doc_id, root = doc
    ids = _create_n(doc_id, 4)
    settings = _settings(root, snapshot_keep_n=1, snapshot_keep_days=30)

    # inside the 30-day window everything stays: union of (last 1) and (all within 30 days)
    result = prune_snapshots_at(root, doc_id, settings, now=datetime.now(UTC))
    assert result.pruned_snapshot_ids == [] and result.retained_count == 4

    # far in the future only the last 1 by count survives
    later = datetime.now(UTC) + timedelta(days=60)
    result = prune_snapshots_at(root, doc_id, settings, now=later)
    assert set(result.pruned_snapshot_ids) == set(ids[:3]) and _remaining(doc_id) == ids[3:]


def test_hard_cap_on_count_trims_inside_the_window(doc) -> None:
    doc_id, root = doc
    ids = _create_n(doc_id, 5)
    settings = _settings(root, snapshot_keep_n=50, snapshot_keep_days=30, snapshot_hard_max_n=2)
    result = prune_snapshots_at(root, doc_id, settings)
    assert set(result.pruned_snapshot_ids) == set(ids[:3]) and _remaining(doc_id) == ids[3:]


def test_hard_cap_on_bytes_trims_inside_the_window(doc) -> None:
    doc_id, root = doc
    ids = _create_n(doc_id, 3)
    settings = _settings(
        root,
        snapshot_keep_n=50,
        snapshot_keep_days=30,
        snapshot_hard_max_bytes=2 * SNAP_BYTES + 1,  # fits 2 of 3 equal snapshots
    )
    result = prune_snapshots_at(root, doc_id, settings)
    assert result.pruned_snapshot_ids == [ids[0]] and _remaining(doc_id) == ids[1:]


def test_original_and_working_copy_are_never_pruned(doc) -> None:
    doc_id, root = doc
    _create_n(doc_id, 4)
    working, original = sandbox.working_copy(root, doc_id), sandbox.original_copy(root, doc_id)
    before = (working.read_bytes(), original.read_bytes())

    prune_snapshots_at(root, doc_id, _settings(root, snapshot_keep_n=1, snapshot_keep_days=0))

    assert (working.read_bytes(), original.read_bytes()) == before


def test_orphaned_operation_records_go_with_their_snapshots(doc) -> None:
    doc_id, root = doc
    ids = _create_n(doc_id, 3)
    reg = get_registry()

    def record(snapshot_id: str | None):
        rec = new_operation(doc_id, tool="x", risk_class=RiskClass.MEDIUM, params={}, registry=reg)
        if snapshot_id:
            update_operation(
                rec, registry=reg, snapshot_id=snapshot_id, status=OperationStatus.APPLIED
            )
        return rec.operation_id

    orphan, kept, unlinked = record(ids[0]), record(ids[2]), record(None)
    result = prune_snapshots_at(
        root, doc_id, _settings(root, snapshot_keep_n=1, snapshot_keep_days=0)
    )

    ops = sandbox.operations_dir(root, doc_id)
    assert orphan in result.pruned_operation_ids and not (ops / f"{orphan}.json").exists()
    assert (ops / f"{kept}.json").exists()  # its snapshot survives
    assert (ops / f"{unlinked}.json").exists()  # no linked snapshot: never orphaned


def test_prune_is_idempotent(doc) -> None:
    doc_id, root = doc
    _create_n(doc_id, 4)
    settings = _settings(root, snapshot_keep_n=2, snapshot_keep_days=0)
    first = prune_snapshots_at(root, doc_id, settings)
    second = prune_snapshots_at(root, doc_id, settings)
    assert len(first.pruned_snapshot_ids) == 2
    assert second.pruned_snapshot_ids == [] and second.freed_bytes == 0


def test_a_tampered_index_entry_cannot_delete_outside_the_snapshot_dir(doc) -> None:
    doc_id, root = doc
    _create_n(doc_id, 1)
    index_path = sandbox.snapshots_index(root, doc_id)
    index = json.loads(index_path.read_text())
    index["snapshots"].insert(
        0,
        {
            "snapshot_id": "snap_00000000",
            "seq": 0,
            "file": "../../evil.svg",  # a prune candidate carrying a path component
            "created_at": datetime.now(UTC).isoformat(),
            "label": None,
            "operation_id": None,
            "size_bytes": 10,
        },
    )
    index_path.write_text(json.dumps(index))

    result = prune_snapshots_at(
        root, doc_id, _settings(root, snapshot_keep_n=1, snapshot_keep_days=0)
    )

    assert "snap_00000000" not in result.pruned_snapshot_ids
    assert "snap_00000000" in _remaining(doc_id)  # stays indexed rather than deleted via the path
    assert not (root / "evil.svg").exists()


def test_empty_index_is_a_noop(doc) -> None:
    doc_id, root = doc
    result = prune_snapshots_at(root, doc_id, _settings(root))
    assert (result.pruned_snapshot_ids, result.retained_count, result.freed_bytes) == ([], 0, 0)


def test_sweep_prunes_every_document_and_is_idempotent(doc) -> None:
    doc_id, root = doc
    _create_n(doc_id, 5)
    settings = _settings(root, snapshot_keep_n=2, snapshot_keep_days=0)

    sweep = sweep_all_roots(settings)

    assert sweep.pruned_snapshots == 3
    assert [d.doc_id for d in sweep.documents] == [doc_id] and len(list_snapshots(doc_id)) == 2
    assert sweep_all_roots(settings).pruned_snapshots == 0


# --- `history(action=prune)` --------------------------------------------------------------------


def test_history_prune_applies_the_configured_policy(
    tool_app, doc, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _ = doc
    ids = _create_n(doc_id, 5)
    monkeypatch.setenv(ENV_SNAPSHOT_KEEP_N, "2")
    monkeypatch.setenv(ENV_SNAPSHOT_KEEP_DAYS, "0")
    get_settings.cache_clear()

    res = call(tool_app, "history", {"doc_id": doc_id, "action": "prune"}).data

    assert (res["pruned_snapshots"], res["retained"], res["freed_bytes"]) == (3, 2, 3 * SNAP_BYTES)
    assert res["doc_id"] == doc_id and "pruned_operations" in res
    assert res["live_frames"]["pruned"] == 0  # the live-frame pass ran and reports its outcome
    assert _remaining(doc_id) == ids[3:]
    again = call(tool_app, "history", {"doc_id": doc_id, "action": "prune"}).data
    assert again["pruned_snapshots"] == 0


# --- live-frame retention (part of the same explicit sweep) -------------------------------------


def _frame(root: Path, name: str, *, age_days: float = 0.0, size: int = FRAME_BYTES) -> Path:
    """A live loop frame under the live artifacts dir, with a controllable mtime."""
    sandbox.ensure_live_dirs(root)
    path = sandbox.live_artifacts_dir(root) / name
    path.write_bytes(b"x" * size)
    if age_days:
        ts = (datetime.now(UTC) - timedelta(days=age_days)).timestamp()
        os.utime(path, (ts, ts))
    return path


def test_live_frames_are_pruned_by_age(doc) -> None:
    _, root = doc
    old = _frame(root, "live-view-old.png", age_days=30)
    fresh = _frame(root, "live-view-fresh.png")
    result = prune_live_frames_at(root, _settings(root, live_frame_keep_days=7))
    assert result.pruned_frames == 1 and not old.exists() and fresh.exists()


def test_live_frames_are_pruned_by_byte_budget_oldest_first(doc) -> None:
    _, root = doc
    oldest = _frame(root, "live-view-a.png", age_days=3)
    _frame(root, "live-view-b.png", age_days=2)
    _frame(root, "live-view-c.png", age_days=1)
    settings = _settings(root, live_frame_keep_days=30, live_frame_max_bytes=2 * FRAME_BYTES + 1)

    result = prune_live_frames_at(root, settings)

    assert result.pruned_frames == 1 and result.retained_frames == 2 and not oldest.exists()


def test_a_frame_referenced_by_an_operation_record_is_kept(doc) -> None:
    _, root = doc
    referenced = _frame(root, "live-view-before.png", age_days=99)
    other = _frame(root, "live-view-stale.png", age_days=99)
    now = datetime.now(UTC).isoformat()
    _persist(
        LiveOperationRecord(
            operation_id="op_00000001",
            tool="live_apply_to_selection",
            risk_class=RiskClass.HIGH,
            params={},
            previews={"before": ".inkscape-mcp/live/artifacts/live-view-before.png"},
            created_at=now,
            updated_at=now,
        ),
        root,
    )

    result = prune_live_frames_at(root, _settings(root, live_frame_keep_days=7))

    assert referenced.exists() and not other.exists() and result.pruned_frames == 1


def test_only_loop_view_frames_are_pruned(doc) -> None:
    _, root = doc
    diff = _frame(root, "live-diff-op_00000001.png", age_days=99)
    selection = _frame(root, "live-selection-x.png", age_days=99)
    view = _frame(root, "live-view-x.png", age_days=99)
    prune_live_frames_at(root, _settings(root, live_frame_keep_days=7))
    assert diff.exists() and selection.exists() and not view.exists()


def test_live_frame_prune_is_idempotent(doc) -> None:
    _, root = doc
    _frame(root, "live-view-old.png", age_days=30)
    settings = _settings(root, live_frame_keep_days=7)
    assert prune_live_frames_at(root, settings).pruned_frames == 1
    assert prune_live_frames_at(root, settings).pruned_frames == 0


def test_the_sweep_and_prune_document_include_live_frames(doc) -> None:
    doc_id, root = doc
    _create_n(doc_id, 1)
    _frame(root, "live-view-old.png", age_days=30)
    _frame(root, "live-view-fresh.png")
    settings = _settings(root, snapshot_keep_n=1, snapshot_keep_days=0, live_frame_keep_days=7)

    swept = prune_document(doc_id, settings=settings)
    assert swept.live_frames is not None and swept.live_frames.pruned_frames == 1

    _frame(root, "live-view-old2.png", age_days=30)
    sweep = sweep_all_roots(settings)
    assert sweep.pruned_live_frames == 1 and sweep.freed_live_bytes == FRAME_BYTES


# --- retention is explicit: no edit and no render ever triggers it ------------------------------


def test_edits_never_prune_snapshots_or_frames(
    tool_app, doc, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root = doc
    ancient = _frame(root, "live-view-ancient.png", age_days=999)
    monkeypatch.setenv(ENV_SNAPSHOT_KEEP_N, "1")  # a policy that WOULD prune if anything ran it
    monkeypatch.setenv(ENV_SNAPSHOT_KEEP_DAYS, "0")
    get_settings.cache_clear()

    for i in range(3):
        op = {"op": "create_rect", "x": i, "y": 0, "width": 1, "height": 1}
        call(tool_app, "edit", {"doc_id": doc_id, "ops": [op]})

    assert len(list_snapshots(doc_id)) == 3 and ancient.exists()


def test_live_renders_never_prune_older_frames(doc, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rendering many frames must accumulate them, not silently delete older ones."""
    _, root = doc
    ancient = _frame(root, "live-view-ancient.png", age_days=999)
    settings = Settings(workspace_roots=[root], live_enabled=True, live_frame_keep_days=7)
    transport = FakeTransport(png=b"\x89PNGframe")
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    monkeypatch.setattr(session_mod, "select_transport", lambda s, required: transport)
    manager = LiveSessionManager(settings)
    manager.connect()
    try:
        for i in range(3):
            transport.state_revision = f"rev-{i}"  # distinct cache keys -> real renders
            render_live_view(manager=manager, settings=settings)
    finally:
        manager.disconnect()

    assert ancient.exists()


def test_restore_of_a_pruned_snapshot_is_actionable_and_changes_nothing(
    tool_app, doc, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root = doc
    oldest = _create_n(doc_id, 3)[0]
    monkeypatch.setenv(ENV_SNAPSHOT_KEEP_N, "1")
    monkeypatch.setenv(ENV_SNAPSHOT_KEEP_DAYS, "0")
    get_settings.cache_clear()
    call(tool_app, "history", {"doc_id": doc_id, "action": "prune"})
    working = sandbox.working_copy(root, doc_id)
    working.write_bytes(SVG.replace(b"10", b"20"))
    before, remaining = working.read_bytes(), _remaining(doc_id)

    with pytest.raises(ToolError, match=r"snapshot not found; call history\(action='list'\)"):
        call(tool_app, "history", {"doc_id": doc_id, "action": "restore", "snapshot_id": oldest})

    assert working.read_bytes() == before and _remaining(doc_id) == remaining
