"""Live sync + render tests: new-doc sync, Operation Record + snapshot, damage-safety."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inkscape_mcp.config import Settings
from inkscape_mcp.live import session as session_mod
from inkscape_mcp.live.render import render_live_view
from inkscape_mcp.live.session import LiveSessionManager
from inkscape_mcp.live.sync import LiveSyncError, sync_live_to_workspace
from inkscape_mcp.registry import Registry
from inkscape_mcp.workspace import sandbox

from .conftest import FakeTransport

FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKE-CANVAS"


@pytest.fixture(autouse=True)
def _quiet_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])


def _connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LiveSessionManager, Settings]:
    settings = Settings(workspace_roots=[tmp_path], live_enabled=True)
    monkeypatch.setattr(
        session_mod, "select_transport", lambda s, required: FakeTransport(png=FAKE_PNG)
    )
    mgr = LiveSessionManager(settings)
    mgr.connect()
    return mgr, settings


def test_sync_writes_new_tracked_doc_with_operation_and_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    reg = Registry(settings)
    dest = tmp_path / "synced.svg"

    result = sync_live_to_workspace(str(dest), manager=mgr, registry=reg, settings=settings)

    # New file written with the live SVG bytes; result path is workspace-relative.
    assert dest.is_file()
    assert dest.read_text().startswith("<svg")
    assert result.saved_path == "synced.svg"
    assert not Path(result.saved_path).is_absolute()

    # Registered as a tracked document with a working copy.
    entry = reg.get(result.doc_id)
    assert Path(entry.working_path).is_file()

    # ADR-004: an applied Operation Record links the snapshot.
    op_dir = sandbox.operations_dir(tmp_path, result.doc_id)
    records = [json.loads(p.read_text()) for p in op_dir.glob("op_*.json")]
    sync_recs = [r for r in records if r["tool"] == "live_sync_to_workspace"]
    assert len(sync_recs) == 1
    assert sync_recs[0]["status"] == "applied"
    assert sync_recs[0]["snapshot_id"] == result.snapshot_id

    # The snapshot file exists in the snapshots dir.
    snaps = list(sandbox.snapshots_dir(tmp_path, result.doc_id).glob("*.svg"))
    assert snaps


def test_sync_never_overwrites_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    reg = Registry(settings)
    dest = tmp_path / "existing.svg"
    dest.write_bytes(b"ORIGINAL")

    with pytest.raises(LiveSyncError):
        sync_live_to_workspace(str(dest), manager=mgr, registry=reg, settings=settings)

    # The existing file is byte-unchanged — a live sync can never damage a workspace file.
    assert dest.read_bytes() == b"ORIGINAL"


def test_sync_rejects_path_outside_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    reg = Registry(settings)
    outside = tmp_path.parent / "escape.svg"
    with pytest.raises(LiveSyncError):
        sync_live_to_workspace(str(outside), manager=mgr, registry=reg, settings=settings)


def test_render_view_writes_png_under_live_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    result = render_live_view(manager=mgr, settings=settings)

    assert result.format == "png"
    assert result.artifact_path.startswith(".inkscape-mcp/live/artifacts/")
    written = tmp_path / result.artifact_path
    assert written.read_bytes() == FAKE_PNG
    assert result.size_bytes == len(FAKE_PNG)


# ---: relative dest anchors to the workspace root, not the process CWD --------


def test_relative_dest_anchors_to_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Run from a CWD that is NOT the workspace root, so a CWD-anchored resolution would fail.
    mgr, settings = _connected(tmp_path, monkeypatch)
    reg = Registry(settings)
    other_cwd = tmp_path.parent / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    result = sync_live_to_workspace("synced-rel.svg", manager=mgr, registry=reg, settings=settings)

    # The file landed under the WORKSPACE root (not the CWD), and the returned path is relative.
    assert (tmp_path / "synced-rel.svg").is_file()
    assert not (other_cwd / "synced-rel.svg").exists()
    assert result.saved_path == "synced-rel.svg"
    assert not Path(result.saved_path).is_absolute()


def test_relative_dest_into_existing_subdir_anchors_to_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    reg = Registry(settings)
    (tmp_path / "out").mkdir()
    monkeypatch.chdir(tmp_path.parent)

    result = sync_live_to_workspace("out/nested.svg", manager=mgr, registry=reg, settings=settings)
    assert (tmp_path / "out" / "nested.svg").is_file()
    assert result.saved_path == "out/nested.svg"


# ---: live sync auto-creates missing parent dirs inside the sandbox ----------


def test_sync_creates_missing_parent_dir_in_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dest whose parent dir does NOT yet exist succeeds — the parent is auto-created in-sandbox.

    Reproduces the reported failure (`path rejected: parent directory not found` → manual
    `mkdir -p` then retry); now matches `save_document_as` parent-creation semantics.
    """
    mgr, settings = _connected(tmp_path, monkeypatch)
    reg = Registry(settings)
    assert not (tmp_path / "out").exists()

    result = sync_live_to_workspace(
        "out/deep/synced.svg", manager=mgr, registry=reg, settings=settings
    )

    # The missing parent chain was created under the workspace root and the file landed inside it.
    assert (tmp_path / "out" / "deep" / "synced.svg").is_file()
    assert result.saved_path == "out/deep/synced.svg"
    assert not Path(result.saved_path).is_absolute()


def test_sync_missing_parent_escape_rejected_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An out-of-sandbox dest with a missing parent is rejected and creates nothing outside."""
    mgr, settings = _connected(tmp_path, monkeypatch)
    reg = Registry(settings)
    # Relative `..`-escape into a not-yet-existing subfolder ABOVE the workspace root.
    with pytest.raises(LiveSyncError) as exc:
        sync_live_to_workspace(
            "../escape-dir/synced.svg", manager=mgr, registry=reg, settings=settings
        )
    assert "outside workspace" in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    # Nothing was created outside the sandbox.
    assert not (tmp_path.parent / "escape-dir").exists()


def test_relative_dest_escaping_workspace_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A relative dest with `..` that escapes the workspace must still be rejected after anchoring.
    mgr, settings = _connected(tmp_path, monkeypatch)
    reg = Registry(settings)
    with pytest.raises(LiveSyncError) as exc:
        sync_live_to_workspace("../escape.svg", manager=mgr, registry=reg, settings=settings)
    assert "outside workspace" in str(exc.value)
    # No host path leaks in the public message.
    assert str(tmp_path) not in str(exc.value)


def test_absolute_out_of_sandbox_dest_still_rejected_no_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    reg = Registry(settings)
    outside = tmp_path.parent / "outside.svg"
    with pytest.raises(LiveSyncError) as exc:
        sync_live_to_workspace(str(outside), manager=mgr, registry=reg, settings=settings)
    assert "outside workspace" in str(exc.value)
    assert str(outside) not in str(exc.value)


# ---: the dest sandbox guard fires independent of connection state -----------


def _disconnected(tmp_path: Path) -> tuple[LiveSessionManager, Settings]:
    """A manager that was NEVER connected — `require_transport` would raise `LiveNotAvailable`."""
    settings = Settings(workspace_roots=[tmp_path], live_enabled=True)
    return LiveSessionManager(settings), settings


def test_dest_sandbox_guard_fires_when_disconnected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    #: an out-of-sandbox absolute dest is rejected by the sandbox guard EVEN WHEN NOT
    # CONNECTED — the guard runs before the transport is required, so the branch is reachable
    # headless. The error names the sandbox rejection (not "no live session"), no host path leaked.
    mgr, settings = _disconnected(tmp_path)
    reg = Registry(settings)
    outside = tmp_path.parent / "escape.svg"
    with pytest.raises(LiveSyncError) as exc:
        sync_live_to_workspace(str(outside), manager=mgr, registry=reg, settings=settings)
    assert "outside workspace" in str(exc.value)
    assert "no live session" not in str(exc.value)
    assert str(outside) not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    assert not outside.exists()


def test_relative_escape_dest_rejected_when_disconnected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A `..`-escaping relative dest is anchored to the workspace root, then rejected — disconnected.
    mgr, settings = _disconnected(tmp_path)
    reg = Registry(settings)
    with pytest.raises(LiveSyncError) as exc:
        sync_live_to_workspace("../escape.svg", manager=mgr, registry=reg, settings=settings)
    assert "outside workspace" in str(exc.value)
    assert "no live session" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
