"""`history`: snapshots (list / create / restore / prune) and reload from the source file."""

from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult

from inkscape_mcp import retention
from inkscape_mcp import snapshots as snapshots_engine
from inkscape_mcp.config import get_settings
from inkscape_mcp.document.save import FileRestoreResult, restore_saved_file
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.registry import get_registry
from inkscape_mcp.tools._result import doc_summary, guarded, ok, require_doc

_logger = get_logger("tools.history")

Action = Literal["list", "create", "restore", "restore_file", "undo", "prune", "reload"]

#: Same cap the old snapshot tool applied.
MAX_LABEL_LEN = 256

DESCRIPTION = (
    "Undo/redo safety net. Every edit snapshots the state BEFORE it. action='undo' (optional "
    "steps=N) reverts the last edit(s); call again to go further back; itself reversible. 'list' "
    "shows snapshots; 'create' checkpoints now (optional label); 'restore' + `snapshot_id` rolls "
    "back to it (reversible); 'restore_file' + `operation_id` + `approval_token` (HIGH) puts back "
    "a file an export save_as overwrote (dry_run=true previews); 'prune' reclaims old snapshots; "
    "'reload' re-reads the source file. To undo, use 'undo' or the `undo` call from a result "
    "verbatim; never guess a snapshot_id. Example: history(doc_id, action='undo')"
)


SNAPSHOT_NOTE = (
    "Each snapshot is the document state BEFORE the edit named in its label. Restoring it returns "
    "to that earlier state, undoing that edit and every later one. Restore is itself reversible."
)


def _state_of(label: str | None, restored: bool = False) -> str:
    """Plain-words state a snapshot holds ('before: X' stays; others are named checkpoints).

    `restored=True` words the edit snapshots for a restore result, so the named edit is not
    misread as part of the restored state ('just before edit 'X' - that edit is undone').
    """
    if label and label.startswith("before:"):
        if restored:
            return (
                f"just before edit '{label.removeprefix('before:').strip()}' — that edit is undone"
            )
        return label
    return f"saved as {label or 'unlabeled'!r}"


def _restore_call(doc_id: str, snapshot_id: str) -> str:
    """Pasteable call, same format as an edit result's `undo`."""
    return f"history(doc_id='{doc_id}', action='restore', snapshot_id='{snapshot_id}')"


def _undoable(snaps: list[snapshots_engine.SnapshotInfo]) -> list[snapshots_engine.SnapshotInfo]:
    """Edit snapshots (label 'before: ...') not yet undone, oldest first; the last one is next.

    Replays the index in order: an edit snapshot is pushed; a restore (its pre-restore snapshot is
    labelled 'pre-restore before <target>') pops every edit at or after the target and remembers
    them under the pre-restore snapshot, so restoring THAT (undoing the undo) pushes them back;
    a reload clears the stack. ponytail: live-sync/checkpoint-only restores are approximated by seq.
    """
    stack: list[snapshots_engine.SnapshotInfo] = []
    redo: dict[str, list[snapshots_engine.SnapshotInfo]] = {}
    by_id = {s.snapshot_id: s for s in snaps}
    for s in sorted(snaps, key=lambda x: x.seq):
        label = s.label or ""
        if s.operation_id and label.startswith("before:"):
            stack.append(s)
        elif label == "pre-reload":
            stack.clear()
        elif label.startswith("pre-restore before "):
            target = by_id.get(label.removeprefix("pre-restore before "))
            if target is None:
                continue
            redo[s.snapshot_id] = [e for e in stack if e.seq >= target.seq]
            stack = [e for e in stack if e.seq < target.seq] + redo.get(target.snapshot_id, [])
    return stack


@guarded(limit_msg="input file exceeds the configured size limit")
def history(
    doc_id: str,
    action: Action = "list",
    snapshot_id: str | None = None,
    label: str | None = None,
    steps: int = 1,
    operation_id: str | None = None,
    approval_token: str | None = None,
    dry_run: bool = False,
) -> ToolResult:
    require_doc(doc_id)
    if action != "restore" and snapshot_id is not None:
        raise ToolError("snapshot_id is only used with action='restore'")
    if action != "create" and label is not None:
        raise ToolError("label is only used with action='create'")
    if action != "undo" and steps != 1:
        raise ToolError("steps is only used with action='undo'")
    if action != "restore_file" and (operation_id is not None or approval_token is not None):
        raise ToolError("operation_id and approval_token are only used with action='restore_file'")
    if dry_run and action != "restore_file":
        raise ToolError("dry_run is only used with action='restore_file'")
    reg = get_registry()
    payload: dict[str, object]

    if action == "list":
        snaps = snapshots_engine.list_snapshots(doc_id, registry=reg)
        payload = {
            "doc_id": doc_id,
            "count": len(snaps),
            "note": SNAPSHOT_NOTE,
            "snapshots": [
                {
                    "id": s.snapshot_id,
                    "seq": s.seq,
                    "label": s.label,
                    "restoring": f"returns to the state {_state_of(s.label)}",
                    "operation_id": s.operation_id,
                    "bytes": s.size_bytes,
                }
                for s in snaps
            ],
        }
    elif action == "create":
        if label is not None and len(label) > MAX_LABEL_LEN:
            raise ToolError(f"snapshot label too long (max {MAX_LABEL_LEN} characters)")
        if label is not None and (
            label == "pre-reload" or label.startswith(("before:", "pre-restore before "))
        ):
            raise ToolError("that label is reserved for the server's own snapshots; pick another")
        info = snapshots_engine.create_snapshot(doc_id, label=label, registry=reg)
        payload = {"doc_id": doc_id, "snapshot_id": info.snapshot_id, "seq": info.seq}
    elif action == "restore":
        if not snapshot_id:
            raise ToolError("action='restore' requires snapshot_id; call history(action='list')")
        res = snapshots_engine.restore_snapshot(doc_id, snapshot_id, registry=reg)
        target = next(
            (
                x
                for x in snapshots_engine.list_snapshots(doc_id, registry=reg)
                if x.snapshot_id == snapshot_id
            ),
            None,
        )
        payload = {
            "doc_id": doc_id,
            "restored_from": res.restored_from,
            "restored_state": f"the state {_state_of(target.label if target else None, True)}",
            "note": "document is now back at that snapshot's state; undo this with the `undo` "
            "call below",
            "undo": _restore_call(doc_id, res.pre_restore_snapshot_id),
            "pre_restore_snapshot_id": res.pre_restore_snapshot_id,
            "operation_id": res.operation_id,
        }
        if target and target.label == "pre-reload":
            payload["warning"] = (
                "this is the pre-reload state: your edits are back but the reloaded outside "
                "change of the source file is dropped; to keep both, re-apply your edits after "
                "a reload."
            )
    elif action == "restore_file":
        if not operation_id:
            raise ToolError("action='restore_file' requires operation_id (from a save result)")
        done = restore_saved_file(doc_id, operation_id, approval_token, dry_run=dry_run)
        if not isinstance(done, FileRestoreResult):
            payload = {
                "dry_run": True,
                "plan": {
                    "path": done.restored_path,
                    "operation_id": done.operation_id,
                    "bytes": done.bytes,
                },
                "risk": "high",
                "needs_approval": True,
            }
            log_tool_call(_logger, tool="history", doc_id=doc_id, action=action)
            return ok(payload)
        payload = {
            "doc_id": doc_id,
            "restored_path": done.restored_path,
            "restored_from": done.restored_from,
            "bytes": done.bytes,
            "note": "the file now has its earlier bytes again; undo this with the `undo` call "
            "below (needs its own approval_token). The working copy (doc_id) is unchanged: it "
            "still has the later edits and may differ from the restored file; save again to "
            "re-apply them.",
            "undo": done.undo,
            "operation_id": done.operation_id,
        }
    elif action == "undo":
        if steps < 1:
            raise ToolError("steps must be >= 1")
        stack = _undoable(snapshots_engine.list_snapshots(doc_id, registry=reg))
        if not stack:
            raise ToolError(
                "nothing to undo: no edit has been made (or all were already undone). "
                "history(action='list') shows snapshots; action='reload' re-reads the source file"
            )
        if steps > len(stack):
            raise ToolError(f"only {len(stack)} edit(s) can be undone; steps={steps} is too many")
        target = stack[-steps]
        res = snapshots_engine.restore_snapshot(doc_id, target.snapshot_id, registry=reg)
        payload = {
            "doc_id": doc_id,
            "undone": [e.label for e in reversed(stack[-steps:])],
            "restored_state": f"the state {_state_of(target.label, True)}",
            "remaining_undoable": len(stack) - steps,
            "note": "call undo again to go one edit further back; to redo, restore "
            "pre_restore_snapshot_id",
            "pre_restore_snapshot_id": res.pre_restore_snapshot_id,
            "operation_id": res.operation_id,
        }
    elif action == "prune":
        pruned = retention.prune_document(doc_id, registry=reg)
        frames = pruned.live_frames
        payload = {
            "doc_id": doc_id,
            "pruned_snapshots": len(pruned.pruned_snapshot_ids),
            "pruned_operations": len(pruned.pruned_operation_ids),
            "retained": pruned.retained_count,
            "freed_bytes": pruned.freed_bytes,
            "live_frames": frames
            and {
                "pruned": frames.pruned_frames,
                "retained": frames.retained_frames,
                "freed_bytes": frames.freed_bytes,
            },
        }
        cfg = get_settings()
        limits = (
            f"keeps the newest {cfg.snapshot_keep_n} snapshots plus any younger than "
            f"{cfg.snapshot_keep_days} days (hard cap {cfg.snapshot_hard_max_n} snapshots)"
        )
        if (
            pruned.pruned_snapshot_ids
            or pruned.pruned_operation_ids
            or (frames and frames.pruned_frames)
        ):
            payload["note"] = (
                f"Removed old snapshots/records beyond the retention limits; prune {limits}."
            )
        else:
            payload["note"] = (
                f"Nothing to prune, not an error: all {pruned.retained_count} snapshot(s) are "
                f"within the retention limits (prune {limits}). Prune only frees old history; it "
                "never touches the document, and undo/restore keep working."
            )
    else:  # reload
        pre = snapshots_engine.create_snapshot(doc_id, label="pre-reload", registry=reg)
        reg.reload(doc_id)
        payload = {
            "doc_id": doc_id,
            "pre_reload_snapshot_id": pre.snapshot_id,
            "summary": doc_summary(doc_id),
            "note": (
                "Restoring pre_reload_snapshot_id brings back your edits but drops the reloaded "
                "outside change; to keep both, re-apply your edits after the reload."
            ),
            "undo": _restore_call(doc_id, pre.snapshot_id),
        }

    log_tool_call(_logger, tool="history", doc_id=doc_id, action=action)
    return ok(payload)


def register(mcp: FastMCP) -> None:
    mcp.tool(name="history", description=DESCRIPTION, output_schema=None)(history)
