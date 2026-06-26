"""Snapshot retention / cleanup tests (E1 follow-up / workspace-model §6)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, Settings, get_settings
from inkscape_mcp.operations import OperationStatus, new_operation, update_operation
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.retention import (
    prune_document,
    prune_live_frames_at,
    prune_snapshots_at,
    sweep_all_roots,
)
from inkscape_mcp.server import mcp
from inkscape_mcp.snapshots import create_snapshot, list_snapshots
from inkscape_mcp.tools.snapshots import prune_snapshots
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.risk import RiskClass

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'
SNAP_BYTES = len(SVG)


@pytest.fixture
def doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    """Open a fixture SVG; return (doc_id, owning_root)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    src = ws / "logo.svg"
    src.write_bytes(SVG)
    entry = get_registry().open_document(str(src))
    return entry.doc_id, ws


def _make_settings(root: Path, **overrides: int) -> Settings:
    """Settings with the given root and retention overrides (defaults otherwise)."""
    return Settings(workspace_roots=[root], **overrides)


def _create_n(doc_id: str, n: int) -> list[str]:
    """Create `n` snapshots, return their ids oldest→newest."""
    return [create_snapshot(doc_id).snapshot_id for _ in range(n)]


def test_keep_n_prunes_oldest(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    ids = _create_n(doc_id, 5)
    snap_dir = sandbox.snapshots_dir(root, doc_id)
    # keep the newest 2 by count; no age keep so the count rule governs.
    settings = _make_settings(root, snapshot_keep_n=2, snapshot_keep_days=0)

    result = prune_snapshots_at(root, doc_id, settings)

    assert set(result.pruned_snapshot_ids) == set(ids[:3])
    assert result.retained_count == 2
    assert result.freed_bytes == 3 * SNAP_BYTES
    # Index now lists only the survivors, in order.
    remaining = [s.snapshot_id for s in list_snapshots(doc_id)]
    assert remaining == ids[3:]
    # Pruned files are gone from disk; survivors remain.
    files = {p.name for p in snap_dir.glob("*.svg")}
    assert len(files) == 2


def test_keep_days_union_with_count(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    ids = _create_n(doc_id, 4)
    settings = _make_settings(root, snapshot_keep_n=1, snapshot_keep_days=30)

    # now == creation time: every snapshot is inside the 30-day window, so the UNION of
    # (last 1 by count) and (all within 30 days) keeps everything.
    result = prune_snapshots_at(root, doc_id, settings, now=datetime.now(UTC))
    assert result.pruned_snapshot_ids == []
    assert result.retained_count == 4

    # Far in the future: all fall outside the 30-day window, so only the last 1 by count stays.
    future = datetime.now(UTC) + timedelta(days=60)
    result = prune_snapshots_at(root, doc_id, settings, now=future)
    assert set(result.pruned_snapshot_ids) == set(ids[:3])
    assert [s.snapshot_id for s in list_snapshots(doc_id)] == ids[3:]


def test_hard_cap_count_trims_inside_window(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    ids = _create_n(doc_id, 5)
    # Generous keep window, but the absolute count cap bounds retention to the newest 2.
    settings = _make_settings(
        root, snapshot_keep_n=50, snapshot_keep_days=30, snapshot_hard_max_n=2
    )
    result = prune_snapshots_at(root, doc_id, settings)
    assert set(result.pruned_snapshot_ids) == set(ids[:3])
    assert [s.snapshot_id for s in list_snapshots(doc_id)] == ids[3:]


def test_hard_cap_bytes_trims_inside_window(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    ids = _create_n(doc_id, 3)
    # Byte budget fits only 2 of the 3 equal-size snapshots → drop the oldest.
    budget = 2 * SNAP_BYTES + 1
    settings = _make_settings(
        root, snapshot_keep_n=50, snapshot_keep_days=30, snapshot_hard_max_bytes=budget
    )
    result = prune_snapshots_at(root, doc_id, settings)
    assert result.pruned_snapshot_ids == [ids[0]]
    assert [s.snapshot_id for s in list_snapshots(doc_id)] == ids[1:]


def test_original_and_working_never_pruned(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    _create_n(doc_id, 4)
    working = sandbox.working_copy(root, doc_id)
    original = sandbox.original_copy(root, doc_id)
    before_working = working.read_bytes()
    before_original = original.read_bytes()

    prune_snapshots_at(root, doc_id, _make_settings(root, snapshot_keep_n=1, snapshot_keep_days=0))

    assert working.read_bytes() == before_working
    assert original.read_bytes() == before_original


def test_orphaned_operation_records_pruned(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    ids = _create_n(doc_id, 3)
    reg = get_registry()

    # Record linked to the OLDEST snapshot (will be pruned) → record must go.
    rec_orphan = new_operation(
        doc_id, tool="x", risk_class=RiskClass.MEDIUM, params={}, registry=reg
    )
    update_operation(rec_orphan, registry=reg, snapshot_id=ids[0], status=OperationStatus.APPLIED)
    # Record linked to the NEWEST snapshot (retained) → record must stay.
    rec_keep = new_operation(doc_id, tool="x", risk_class=RiskClass.MEDIUM, params={}, registry=reg)
    update_operation(rec_keep, registry=reg, snapshot_id=ids[2], status=OperationStatus.APPLIED)
    # Record with no linked snapshot → never orphaned, must stay.
    rec_null = new_operation(doc_id, tool="x", risk_class=RiskClass.MEDIUM, params={}, registry=reg)

    ops_dir = sandbox.operations_dir(root, doc_id)
    result = prune_snapshots_at(
        root, doc_id, _make_settings(root, snapshot_keep_n=1, snapshot_keep_days=0)
    )

    assert rec_orphan.operation_id in result.pruned_operation_ids
    assert not (ops_dir / f"{rec_orphan.operation_id}.json").exists()
    assert (ops_dir / f"{rec_keep.operation_id}.json").exists()
    assert (ops_dir / f"{rec_null.operation_id}.json").exists()


def test_prune_is_idempotent(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    _create_n(doc_id, 4)
    settings = _make_settings(root, snapshot_keep_n=2, snapshot_keep_days=0)
    first = prune_snapshots_at(root, doc_id, settings)
    second = prune_snapshots_at(root, doc_id, settings)
    assert len(first.pruned_snapshot_ids) == 2
    assert second.pruned_snapshot_ids == []
    assert second.freed_bytes == 0


def test_tampered_basename_entry_not_deleted(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    _create_n(doc_id, 1)
    # Inject an oldest entry whose `file` carries a path component; it is a prune candidate
    # under keep_n=1 but must NOT be deleted via that path — it stays indexed instead.
    index_path = sandbox.snapshots_index(root, doc_id)
    index = json.loads(index_path.read_text())
    index["snapshots"].insert(
        0,
        {
            "snapshot_id": "snap_00000000",
            "seq": 0,
            "file": "../../evil.svg",
            "created_at": datetime.now(UTC).isoformat(),
            "label": None,
            "operation_id": None,
            "size_bytes": 10,
        },
    )
    index_path.write_text(json.dumps(index))

    result = prune_snapshots_at(
        root, doc_id, _make_settings(root, snapshot_keep_n=1, snapshot_keep_days=0)
    )

    assert "snap_00000000" not in result.pruned_snapshot_ids
    assert "snap_00000000" in {s.snapshot_id for s in list_snapshots(doc_id)}
    assert not (root / "evil.svg").exists()


def test_sweep_all_roots_prunes_every_document(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    _create_n(doc_id, 5)
    settings = _make_settings(root, snapshot_keep_n=2, snapshot_keep_days=0)

    sweep = sweep_all_roots(settings)
    assert sweep.pruned_snapshots == 3
    assert len(sweep.documents) == 1
    assert sweep.documents[0].doc_id == doc_id
    assert len(list_snapshots(doc_id)) == 2

    # Idempotent: a second sweep prunes nothing.
    again = sweep_all_roots(settings)
    assert again.pruned_snapshots == 0


def test_prune_document_resolves_via_registry(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    _create_n(doc_id, 3)
    settings = _make_settings(root, snapshot_keep_n=1, snapshot_keep_days=0)
    result = prune_document(doc_id, settings=settings)
    assert result.retained_count == 1


def test_prune_empty_index_is_noop(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    result = prune_snapshots_at(root, doc_id, _make_settings(root))
    assert result.pruned_snapshot_ids == []
    assert result.retained_count == 0
    assert result.freed_bytes == 0


def test_prune_tool_unknown_doc_raises(doc: tuple[str, Path]) -> None:
    with pytest.raises(ToolError) as exc:
        prune_snapshots("d_nope")
    assert "not found" in str(exc.value)


def test_prune_tool_registered_on_mcp(doc: tuple[str, Path]) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "prune_snapshots" in names


# --- E8-06 live-frame retention (folded into the EXPLICIT sweep) -------------

FRAME_BYTES = 1024


def _write_frame(root: Path, name: str, *, age_days: float = 0.0, size: int = FRAME_BYTES) -> Path:
    """Write a live loop frame under the live artifacts dir with a controllable mtime."""
    sandbox.ensure_live_dirs(root)
    path = sandbox.live_artifacts_dir(root) / name
    path.write_bytes(b"x" * size)
    if age_days:
        ts = (datetime.now(UTC) - timedelta(days=age_days)).timestamp()
        import os

        os.utime(path, (ts, ts))
    return path


def test_live_frames_pruned_by_age(doc: tuple[str, Path]) -> None:
    _, root = doc
    old = _write_frame(root, "live-view-old.png", age_days=30)
    fresh = _write_frame(root, "live-view-fresh.png", age_days=0)
    settings = _make_settings(root, live_frame_keep_days=7)

    result = prune_live_frames_at(root, settings)

    assert result.pruned_frames == 1
    assert not old.exists()
    assert fresh.exists()


def test_live_frames_pruned_by_byte_budget(doc: tuple[str, Path]) -> None:
    _, root = doc
    # Three equal frames, newest first; a budget that fits only two → drop the oldest.
    f_old = _write_frame(root, "live-view-a.png", age_days=3, size=FRAME_BYTES)
    _write_frame(root, "live-view-b.png", age_days=2, size=FRAME_BYTES)
    _write_frame(root, "live-view-c.png", age_days=1, size=FRAME_BYTES)
    settings = _make_settings(
        root, live_frame_keep_days=30, live_frame_max_bytes=2 * FRAME_BYTES + 1
    )

    result = prune_live_frames_at(root, settings)

    assert result.pruned_frames == 1
    assert not f_old.exists()  # oldest dropped to fit the budget
    assert result.retained_frames == 2


def test_live_frame_referenced_by_record_is_kept(doc: tuple[str, Path]) -> None:
    _, root = doc
    from inkscape_mcp.live.records import LiveOperationRecord, _persist

    referenced = _write_frame(root, "live-view-before.png", age_days=99)
    other = _write_frame(root, "live-view-stale.png", age_days=99)
    # A Live Operation Record points at the "before" frame → it must never be pruned.
    rec = LiveOperationRecord(
        operation_id="op_00000001",
        tool="live_apply_to_selection",
        risk_class=RiskClass.HIGH,
        params={},
        previews={"before": ".inkscape-mcp/live/artifacts/live-view-before.png"},
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )
    _persist(rec, root)

    result = prune_live_frames_at(root, _make_settings(root, live_frame_keep_days=7))

    assert referenced.exists()  # protected by the record
    assert not other.exists()  # unreferenced + too old → pruned
    assert result.pruned_frames == 1


def test_live_frame_prune_leaves_non_view_artifacts(doc: tuple[str, Path]) -> None:
    _, root = doc
    diff = _write_frame(root, "live-diff-op_00000001.png", age_days=99)
    sel = _write_frame(root, "live-selection-x.png", age_days=99)
    view = _write_frame(root, "live-view-x.png", age_days=99)

    prune_live_frames_at(root, _make_settings(root, live_frame_keep_days=7))

    # Only loop view frames are pruned; diff/selection artifacts are left alone.
    assert diff.exists()
    assert sel.exists()
    assert not view.exists()


def test_sweep_prunes_live_frames(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    _create_n(doc_id, 1)
    _write_frame(root, "live-view-old.png", age_days=30)
    _write_frame(root, "live-view-fresh.png", age_days=0)
    settings = _make_settings(root, snapshot_keep_n=1, snapshot_keep_days=0, live_frame_keep_days=7)

    sweep = sweep_all_roots(settings)

    assert sweep.pruned_live_frames == 1
    assert sweep.freed_live_bytes == FRAME_BYTES


def test_live_frame_prune_idempotent(doc: tuple[str, Path]) -> None:
    _, root = doc
    _write_frame(root, "live-view-old.png", age_days=30)
    settings = _make_settings(root, live_frame_keep_days=7)
    first = prune_live_frames_at(root, settings)
    second = prune_live_frames_at(root, settings)
    assert first.pruned_frames == 1
    assert second.pruned_frames == 0


def test_prune_tool_also_reports_live_frames(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    _create_n(doc_id, 1)
    _write_frame(root, "live-view-old.png", age_days=30)
    settings = _make_settings(root, live_frame_keep_days=7)

    result = prune_document(doc_id, settings=settings)

    assert result.live_frames is not None
    assert result.live_frames.pruned_frames == 1


def test_mutating_tool_never_prunes_live_frames(doc: tuple[str, Path]) -> None:
    """A live render (the rapid-fire mutating-adjacent call) NEVER triggers retention implicitly.

    Retention is EXPLICIT only (boot sweep + prune tool) — CLAUDE.md hard rule. Rendering many
    frames must accumulate them, not silently prune older ones.
    """
    from inkscape_mcp.live import session as session_mod
    from inkscape_mcp.live.render import render_live_view
    from inkscape_mcp.live.session import LiveSessionManager

    from .conftest import FakeTransport

    _, root = doc
    # Pre-seed an OLD frame that retention WOULD prune if anything implicit ran.
    old = _write_frame(root, "live-view-ancient.png", age_days=999)
    monkeypatch_settings = Settings(
        workspace_roots=[root], live_enabled=True, live_frame_keep_days=7
    )
    session_mod_probe = session_mod.probe_transports
    session_mod.probe_transports = lambda settings=None: []  # type: ignore[assignment]
    transport = FakeTransport(png=b"\x89PNGframe")
    session_mod.select_transport = lambda s, required: transport  # type: ignore[assignment]
    try:
        mgr = LiveSessionManager(monkeypatch_settings)
        mgr.connect()
        for i in range(3):
            transport.state_revision = f"rev-{i}"  # force distinct keys → real renders
            render_live_view(manager=mgr, settings=monkeypatch_settings)
        mgr.disconnect()
    finally:
        session_mod.probe_transports = session_mod_probe  # type: ignore[assignment]

    # The ancient frame is still there — no mutating/render call pruned it implicitly.
    assert old.exists()
