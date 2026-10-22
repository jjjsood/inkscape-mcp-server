"""`history`: snapshots (list / create / restore / prune) and reload from the source file."""

from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult

from inkscape_mcp import retention
from inkscape_mcp import snapshots as snapshots_engine
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.registry import get_registry
from inkscape_mcp.tools._result import doc_summary, guarded, ok, require_doc

_logger = get_logger("tools.history")

Action = Literal["list", "create", "restore", "prune", "reload"]

#: Same cap the old snapshot tool applied.
MAX_LABEL_LEN = 256

DESCRIPTION = (
    "Undo/redo safety net. Every edit already writes one snapshot of the state BEFORE that edit "
    "(restoring it undoes that edit and all later ones). action='list' shows them; "
    "'create' checkpoints now (optional label); 'restore' rolls the working copy back to "
    "`snapshot_id` (itself reversible: it snapshots first); 'prune' reclaims old snapshots and "
    "live frames; 'reload' re-reads the source file under the same doc_id (pre-reload snapshot "
    "taken). "
    "Example: history(doc_id, action='restore', snapshot_id='snap-0003')"
)


SNAPSHOT_NOTE = (
    "Each snapshot is the document state BEFORE the edit named in its label. Restoring it returns "
    "to that earlier state, undoing that edit and every later one. Restore is itself reversible."
)


def _state_of(label: str | None) -> str:
    """Plain-words state a snapshot holds ('before: X' stays; others are named checkpoints)."""
    if label and label.startswith("before:"):
        return label
    return f"saved as {label or 'unlabeled'!r}"


@guarded(limit_msg="input file exceeds the configured size limit")
def history(
    doc_id: str,
    action: Action = "list",
    snapshot_id: str | None = None,
    label: str | None = None,
) -> ToolResult:
    require_doc(doc_id)
    if action != "restore" and snapshot_id is not None:
        raise ToolError("snapshot_id is only used with action='restore'")
    if action != "create" and label is not None:
        raise ToolError("label is only used with action='create'")
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
        info = snapshots_engine.create_snapshot(doc_id, label=label, registry=reg)
        payload = {"doc_id": doc_id, "snapshot_id": info.snapshot_id, "seq": info.seq}
    elif action == "restore":
        if not snapshot_id:
            raise ToolError("action='restore' requires snapshot_id; call history(action='list')")
        res = snapshots_engine.restore_snapshot(doc_id, snapshot_id, registry=reg)
        payload = {
            "doc_id": doc_id,
            "restored_from": res.restored_from,
            "note": "document is now back at that snapshot's state; undo this with "
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
    else:  # reload
        pre = snapshots_engine.create_snapshot(doc_id, label="pre-reload", registry=reg)
        reg.reload(doc_id)
        payload = {
            "doc_id": doc_id,
            "pre_reload_snapshot_id": pre.snapshot_id,
            "summary": doc_summary(doc_id),
        }

    log_tool_call(_logger, tool="history", doc_id=doc_id, action=action)
    return ok(payload)


def register(mcp: FastMCP) -> None:
    mcp.tool(name="history", description=DESCRIPTION, output_schema=None)(history)
