"""Sync the live document into the workspace (medium risk).

`sync_live_to_workspace` is the only mutating piece of: it reads the current SVG from the
connected transport and persists it as a NEW workspace document so the live instance is not the
only source of truth (architecture §4.5). A RELATIVE `dest_path` is anchored to the first workspace
root; when it names a not-yet-existing subfolder the missing parent dirs are auto-created
INSIDE the sandbox first (matching `save_document_as`). It then writes through the §3 policy
layer (`resolve_write_path` + containment/symlink guard), registers the file via `open_document`
(working copy) and mirrors the change as an Operation Record + snapshot (ADR-004).

Damage-safety (epic Done-when): the destination must be a NEW path — an existing file is refused,
never overwritten. The SVG is written to a temp file and atomically `os.replace`d into place, so a
live read that fails mid-transfer (or a fault anywhere before the replace) can never partially
overwrite or corrupt a workspace file.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from pydantic import BaseModel

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.live.session import LiveSessionManager, get_session_manager
from inkscape_mcp.live.transport import LiveError
from inkscape_mcp.logging_setup import get_logger, log_file_io
from inkscape_mcp.operations import OperationStatus, new_operation, update_operation
from inkscape_mcp.registry import Registry, get_registry
from inkscape_mcp.snapshots import create_snapshot
from inkscape_mcp.workspace.limits import LimitExceeded
from inkscape_mcp.workspace.paths import (
    SandboxViolation,
    anchor_to_root,
    ensure_parent_within_sandbox,
    owning_root,
    resolve_write_path,
)
from inkscape_mcp.workspace.risk import RiskClass

_logger = get_logger("live.sync")


class LiveSyncResult(BaseModel):
    """Outcome of `live_sync_to_workspace`.

    `saved_path` is workspace-relative (never an absolute host path). `doc_id` is the freshly
    registered document; `operation_id`/`snapshot_id` are the ADR-004 traceability links.
    """

    doc_id: str
    saved_path: str
    operation_id: str
    snapshot_id: str
    size_bytes: int


class LiveSyncError(Exception):
    """Sync could not complete (read failure, bad destination, or write fault).

    Public message is stable and host-path-free.
    """


def sync_live_to_workspace(
    dest_path: str,
    manager: LiveSessionManager | None = None,
    registry: Registry | None = None,
    settings: Settings | None = None,
) -> LiveSyncResult:
    """Persist the live document SVG to `dest_path` as a new tracked workspace document.

    Order (fail-safe): resolve+containment-check the destination → refuse if it already exists →
    require the transport and read SVG → atomic temp-write+replace → register via `open_document` →
    Operation Record (medium) + snapshot → mark applied. Raises `LiveSyncError` on any failure;
    nothing partial is left behind.

    Destination validation runs BEFORE the transport is required, so an out-of-sandbox or
    symlinked `dest_path` is rejected with `path rejected: outside workspace` even when DISCONNECTED
    — the sandbox guard is no longer shadowed by the connection check, and the branch is exercisable
    headless. Resolving the path writes nothing; it only canonicalizes + sandbox/symlink-checks.
    """
    s = settings if settings is not None else get_settings()
    mgr = manager if manager is not None else get_session_manager()
    reg = registry if registry is not None else get_registry()

    # 1. Anchor a RELATIVE dest to the workspace root (NOT the process CWD) before the sandbox
    # check (shared relative-dest contract). An absolute dest is passed through unchanged;
    # an out-of-sandbox dest is rejected by `resolve_write_path` below — and this happens BEFORE any
    # transport/live work, so the guard holds regardless of connection state.
    anchored = anchor_to_root(dest_path, s)

    # 2. Create any missing parent dirs INSIDE the sandbox, then resolve the destination
    #    through the sandbox write-path choke point (§3 / sec.12). Parent creation proves
    #    containment BEFORE any mkdir (TOCTOU-safe, sec.12), so a `..`-escaping or absolute-outside
    #    dest creates nothing and is rejected with `path rejected: outside workspace` — matching
    #    `save_document_as`. Both steps happen BEFORE any transport/live work, so the
    #    guard holds regardless of connection state. Neither step writes the synced file.
    try:
        ensure_parent_within_sandbox(anchored, s)
        resolved = resolve_write_path(anchored, s)
    except SandboxViolation as exc:
        raise LiveSyncError(exc.args[0]) from exc

    # 3. Never overwrite an existing file (damage-safety): sync only ever creates a new file. The
    #    atomic O_EXCL write below is the authoritative guard; this is the fast, friendly pre-check.
    if resolved.exists():
        raise LiveSyncError("destination already exists; live sync never overwrites")

    # 4. Require a live transport and read the live SVG. If either fails, the already-validated
    #    destination is never touched.
    transport = mgr.require_transport()
    try:
        svg = transport.get_document_svg()
    except LiveError as exc:
        raise LiveSyncError("could not read the live document") from exc

    data = svg.encode("utf-8")
    if len(data) > s.max_input_bytes:
        raise LiveSyncError("live document exceeds the configured size limit")

    # 5. Atomic write: temp file in the same dir, then os.replace into place. Open the temp file
    #    with O_CREAT|O_EXCL|O_NOFOLLOW so the write can never follow a symlink planted at the
    #    (server-minted) temp name and never clobber a pre-existing file — defence in depth behind
    #    resolve_write_path's containment + symlink guard (sec.12 / SV5; mirrors tools/save.py).
    tmp = resolved.with_name(f"{resolved.name}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(tmp, flags, 0o644)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        # rename(2) replaces the destination NAME and does not follow a symlink at `resolved`: a
        # symlink raced into place is destroyed (not written through), so the file always lands in
        # the sandbox-validated location — no escape via the replace (sec.12).
        tmp.replace(resolved)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise LiveSyncError("failed to write the synced document") from exc

    # 6. Register the new file (creates the working copy) and record the operation + snapshot.
    try:
        entry = reg.open_document(resolved)
    except (SandboxViolation, LimitExceeded, ValueError) as exc:
        raise LiveSyncError("synced document could not be registered") from exc

    record = new_operation(
        entry.doc_id,
        tool="live_sync_to_workspace",
        risk_class=RiskClass.MEDIUM,
        params={"dest_path": dest_path, "transport": transport.name},
        registry=reg,
    )
    snapshot = create_snapshot(
        entry.doc_id,
        label="live sync",
        operation_id=record.operation_id,
        registry=reg,
    )

    base = owning_root(resolved, s.workspace_roots) or Path(entry.root)
    try:
        rel = resolved.relative_to(base).as_posix()
    except ValueError:  # pragma: no cover - containment guarantees this holds
        rel = resolved.name

    update_operation(
        record,
        registry=reg,
        snapshot_id=snapshot.snapshot_id,
        artifacts=[rel],
        status=OperationStatus.APPLIED,
    )
    log_file_io(
        _logger,
        action="live_sync_to_workspace",
        doc_id=entry.doc_id,
        operation_id=record.operation_id,
        snapshot_id=snapshot.snapshot_id,
        bytes_written=len(data),
    )
    return LiveSyncResult(
        doc_id=entry.doc_id,
        saved_path=rel,
        operation_id=record.operation_id,
        snapshot_id=snapshot.snapshot_id,
        size_bytes=len(data),
    )
