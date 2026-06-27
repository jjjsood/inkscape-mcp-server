"""Snapshot tools: `create_snapshot` / `list_snapshots` / `restore_snapshot`.

Thin MCP layer over the reusable snapshot engine (`inkscape_mcp.snapshots`). The engine does
all path safety, indexing, and the ADR-004 restore chain; this layer only maps engine
exceptions to `ToolError` with stable, host-path-free messages (sec.12) and logs the call.

Risk classes (tool-catalog): `create_snapshot` / `list_snapshots` are low (write-new snapshot /
read-only manifest; original untouched); `restore_snapshot` is medium and therefore creates an
Operation Record (handled inside the engine). `prune_snapshots` is low (explicit retention sweep:
deletes only superseded snapshot files + orphaned records per workspace-model §6; never touches
the working copy or original).
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from inkscape_mcp.document.inspect import DocumentNotFound
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.retention import PruneResult
from inkscape_mcp.retention import prune_document as _prune_document
from inkscape_mcp.server import mcp
from inkscape_mcp.snapshots import (
    RestoreResult,
    SnapshotInfo,
    SnapshotList,
    SnapshotNotFound,
)
from inkscape_mcp.snapshots import (
    create_snapshot as _create_snapshot,
)
from inkscape_mcp.snapshots import (
    list_snapshots as _list_snapshots,
)
from inkscape_mcp.snapshots import (
    restore_snapshot as _restore_snapshot,
)

_logger = get_logger("tools.snapshots")

#: Max snapshot label length — bounds what a client can write into `index.json` per snapshot.
_MAX_LABEL_LEN = 256


@mcp.tool
def create_snapshot(doc_id: str, label: str | None = None) -> SnapshotInfo:
    """Snapshot the current working copy of a document and index it.

    When to use: checkpointing before a risky edit so you can roll back. To browse checkpoints use
    `list_snapshots`; to roll back use `restore_snapshot`. (Mutating tools auto-snapshot; this is an
    explicit, manual checkpoint.)

    Key params: optional `label` tags the snapshot (length-bounded; over the cap is rejected).

    Return shape: `SnapshotInfo` — the new `snapshot_id` plus its metadata.

    Example: `create_snapshot(doc_id, label="before cleanup")`

    Risk class: low (write-new snapshot; original untouched).
    """
    if label is not None and len(label) > _MAX_LABEL_LEN:
        raise ToolError("snapshot label too long")
    try:
        info = _create_snapshot(doc_id, label=label)
    except (KeyError, DocumentNotFound) as exc:
        raise ToolError("document id not found") from exc

    log_tool_call(_logger, tool="create_snapshot", doc_id=doc_id, snapshot_id=info.snapshot_id)
    return info


@mcp.tool
def list_snapshots(doc_id: str) -> SnapshotList:
    """List a document's snapshots in order, with metadata.

    When to use: choosing which checkpoint to roll back to. To make one use `create_snapshot`; to
    roll back use `restore_snapshot`.

    Key params: none beyond `doc_id`.

    Return shape: `SnapshotList` — `doc_id` plus `snapshots` (each a `SnapshotInfo` with its id +
    metadata), in order.

    Example: `list_snapshots(doc_id)`

    Risk class: low (read-only manifest).
    """
    try:
        snapshots = _list_snapshots(doc_id)
    except (KeyError, DocumentNotFound) as exc:
        raise ToolError("document id not found") from exc

    log_tool_call(_logger, tool="list_snapshots", doc_id=doc_id, count=len(snapshots))
    return SnapshotList(doc_id=doc_id, snapshots=snapshots)


@mcp.tool
def restore_snapshot(doc_id: str, snapshot_id: str) -> RestoreResult:
    """Revert a document's working copy to a chosen snapshot.

    When to use: undoing/rolling back to an earlier checkpoint. To find a `snapshot_id` use
    `list_snapshots`; to make a new checkpoint use `create_snapshot`.

    Key params: `snapshot_id` names the target checkpoint (must exist for this document).

    Return shape: `RestoreResult` — the reversibility-chain links plus `restored_sha256` (SHA-256
    hex digest) and `restored_size_bytes` of the restored working copy, so a caller can assert
    recovery succeeded without reading the document off disk.

    Example: `restore_snapshot(doc_id, snapshot_id)`

    Risk class: medium (reverts working copy via Operation Record; never touches the original).
    """
    try:
        result = _restore_snapshot(doc_id, snapshot_id)
    except (KeyError, DocumentNotFound) as exc:
        raise ToolError("document id not found") from exc
    except SnapshotNotFound as exc:
        raise ToolError("snapshot not found") from exc

    log_tool_call(
        _logger,
        tool="restore_snapshot",
        doc_id=doc_id,
        restored_from=snapshot_id,
        operation_id=result.operation_id,
    )
    return result


@mcp.tool
def prune_snapshots(doc_id: str) -> PruneResult:
    """Apply the snapshot + live-frame retention policy, pruning superseded server state.

       When to use: reclaiming disk from old snapshots/frames. To roll back instead use
       `restore_snapshot`; to list checkpoints use `list_snapshots`. No mutating tool triggers this
       implicitly — it is an explicit maintenance sweep.

       Key params: none beyond `doc_id`. Retains the last N snapshots and all within the keep-days
       window (configurable), bounded by absolute hard caps on count and bytes; deletes the rest plus
       orphaned Operation Records. In the SAME pass it prunes the doc root's loop/live render frames
    by age + byte budget, never deleting a frame still referenced by a Live Operation
       Record. The current working copy and original are never touched, so the restore chain stays
       intact.

       Return shape: `PruneResult` — `pruned_snapshot_ids`, `pruned_operation_ids`, and `live_frames`
       (the frame pruning stats).

       Example: `prune_snapshots(doc_id)`

       Risk class: low (deletes only disposable, superseded server state under a deterministic policy;
       authoritative current state is never affected).
    """
    try:
        result = _prune_document(doc_id)
    except (KeyError, DocumentNotFound) as exc:
        raise ToolError("document id not found") from exc

    log_tool_call(
        _logger,
        tool="prune_snapshots",
        doc_id=doc_id,
        pruned_snapshots=len(result.pruned_snapshot_ids),
        pruned_operations=len(result.pruned_operation_ids),
    )
    return result
