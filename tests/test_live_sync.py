"""Live sync + render tests: new-doc sync, Operation Record + snapshot, damage-safety."""

from __future__ import annotations

import json
import os
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
    monkeypatch.chdir(tmp_path.parent)  # nested dest anchors to the workspace, not the CWD

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


# --- size cap + symlinked destination ------------------------------------------


def test_sync_refuses_live_svg_over_input_cap_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    settings = settings.model_copy(update={"max_input_bytes": 8})
    reg = Registry(settings)
    before = set(tmp_path.rglob("*"))

    with pytest.raises(LiveSyncError, match="size limit"):
        sync_live_to_workspace("big.svg", manager=mgr, registry=reg, settings=settings)

    assert set(tmp_path.rglob("*")) == before  # no dest, no temp file, no record


@pytest.mark.parametrize("target_inside", [False, True], ids=["outside", "inside"])
def test_sync_refuses_symlinked_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_inside: bool
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    reg = Registry(settings)
    base = tmp_path if target_inside else tmp_path.parent
    target = base / f"{tmp_path.name}-target.svg"
    link = tmp_path / "link.svg"
    link.symlink_to(target)  # dangling: a write through it would create `target`

    with pytest.raises(LiveSyncError):
        sync_live_to_workspace(str(link), manager=mgr, registry=reg, settings=settings)

    assert link.is_symlink()
    assert not target.exists()


@pytest.mark.parametrize("dest", [".inkscape-mcp/x.svg", ".INKSCAPE-MCP/new/x.svg"])
def test_sync_refuses_state_dir_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dest: str
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    before = set(tmp_path.rglob("*"))
    with pytest.raises(LiveSyncError, match="managed"):
        sync_live_to_workspace(dest, manager=mgr, registry=Registry(settings), settings=settings)
    assert set(tmp_path.rglob("*")) == before


def test_sync_does_not_clobber_file_raced_in_after_precheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    dest = tmp_path / "raced.svg"
    transport = mgr.require_transport()
    real_get = transport.get_document_svg

    def racing_get() -> str:
        dest.write_bytes(b"RACED")  # appears between the lexists check and placement
        return real_get()

    monkeypatch.setattr(transport, "get_document_svg", racing_get)
    with pytest.raises(LiveSyncError, match="already exists"):
        sync_live_to_workspace(
            str(dest), manager=mgr, registry=Registry(settings), settings=settings
        )
    assert dest.read_bytes() == b"RACED"
    assert list(tmp_path.glob("*.tmp")) == []


def test_sync_falls_back_to_replace_without_hardlinks_or_nofollow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    monkeypatch.delattr(os, "O_NOFOLLOW")

    def no_link(src: object, dst: object) -> None:
        raise PermissionError("hard links unsupported")

    monkeypatch.setattr(os, "link", no_link)
    result = sync_live_to_workspace(
        "fb.svg", manager=mgr, registry=Registry(settings), settings=settings
    )
    assert (tmp_path / "fb.svg").read_text().startswith("<svg")
    assert result.saved_path == "fb.svg"
    assert list(tmp_path.glob("*.tmp")) == []


def test_sync_removes_file_when_registration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    reg = Registry(settings)

    def boom(path: object) -> None:
        raise ValueError("bad")

    monkeypatch.setattr(reg, "open_document", boom)
    with pytest.raises(LiveSyncError, match="registered"):
        sync_live_to_workspace("reg.svg", manager=mgr, registry=reg, settings=settings)
    assert not (tmp_path / "reg.svg").exists()
