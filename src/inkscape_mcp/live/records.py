"""Live Operation Records (ADR-004).

Governance + observability for live mutations. Every mutating live tool opens a
`LiveOperationRecord` that captures WHAT changed, on WHICH selection/objects, via WHICH transport,
plus the before/after canvas renders and the risk-policy decision — the live analogue of the
headless `OperationRecord` scaffold. Records are root-scoped (a live session has no
registered `doc_id`) and persisted append-only under `<root>/.inkscape-mcp/live/operations/`.

This module also enforces the live approval gate: `new_live_operation` runs `enforce_risk_policy`
FIRST, so a HIGH-risk mutation without an `approval_token` raises `PolicyViolation` BEFORE the
caller ever touches the running instance — live mode never mutates unapproved (sec.12 / X1).
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.live.transport import LiveDocumentRef, LiveError
from inkscape_mcp.operations import OperationStatus
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.paths import owning_root
from inkscape_mcp.workspace.risk import RiskClass, enforce_risk_policy

#: Minted operation-id shape (`op_` + 8 hex chars). Shape-validated before any path construction
#: so a caller-supplied id can never traverse out of the live `operations/` dir (mirrors).
_OPERATION_ID_RE = re.compile(r"^op_[0-9a-f]{8}$")

#: Opaque stand-in persisted in place of a live document path that lies OUTSIDE the workspace
#: sandbox. The running Inkscape reports the user's own on-disk path (a host path); persisting it
#: verbatim would leak it through the `live/operations` resource (sec.12 — no host paths in output,
#: R14). We keep the human-readable `name` (a base filename the user already exposed by
#: connecting) but never the absolute host path.
_OPAQUE_DOCUMENT_PATH = "<external>"


class LiveRecordError(LiveError):
    """A Live Operation Record could not be persisted/loaded (e.g. no workspace root)."""


class LiveOperationRecord(BaseModel):
    """Append-only record of one live mutation (ADR-004, live variant).

    Fields capture the full provenance: the `transport` used, the live `document` identity, the
    `selection` at mutation time, the `policy_decision` (incl. approval), the resulting
    `affected_ids`, whether it ran as an `undo_friendly` Inkscape step, and the before/after
    canvas render paths in `previews` (keyed `"before"` / `"after"`, workspace-relative).
    `diff_artifacts` lists any focused before/after visual-diff overlays produced for this op by
    `live_diff_view` (workspace-relative; appended, never replacing the source previews).
    """

    operation_id: str
    tool: str
    risk_class: RiskClass
    params: dict[str, Any]
    transport: str | None = None
    document: LiveDocumentRef | None = None
    selection: list[str] = Field(default_factory=list)
    policy_decision: dict[str, Any] = Field(default_factory=dict)
    affected_ids: list[str] = Field(default_factory=list)
    undo_friendly: bool = False
    previews: dict[str, str] = Field(default_factory=dict)
    diff_artifacts: list[str] = Field(default_factory=list)
    status: OperationStatus = OperationStatus.PROPOSED
    created_at: str
    updated_at: str


class LiveOperationLog(BaseModel):
    """Read-only view of recent Live Operation Records (the `live/operations` resource)."""

    operations: list[LiveOperationRecord] = Field(default_factory=list)
    count: int = 0


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _mint_operation_id() -> str:
    return f"op_{secrets.token_hex(4)}"


def _first_root(settings: Settings) -> Path:
    if not settings.workspace_roots:
        raise LiveRecordError("no workspace root configured to record the live operation")
    return settings.workspace_roots[0]


def _sanitize_document(
    document: LiveDocumentRef | None, settings: Settings
) -> LiveDocumentRef | None:
    """Strip any host path off a live document ref BEFORE it is persisted (sec.12).

    The running Inkscape reports the user's own on-disk document path; persisting it verbatim would
    surface a host path outside the sandbox through the `live/operations` resource. Rewrite `path`
    to a workspace-relative form when the document happens to live inside a configured root, else to
    an opaque marker. The `name` (a base filename the user already exposed by connecting) is kept.
    """
    if document is None or document.path is None:
        return document
    rewritten: str = _OPAQUE_DOCUMENT_PATH
    try:
        resolved = Path(document.path).resolve(strict=False)
        root = owning_root(resolved, settings.workspace_roots)
        if root is not None:
            rewritten = resolved.relative_to(root).as_posix()
    except (OSError, ValueError):  # unresolvable / cross-root → stay opaque, never the host path
        rewritten = _OPAQUE_DOCUMENT_PATH
    return document.model_copy(update={"path": rewritten})


def _record_path(root: Path, operation_id: str) -> Path:
    """Resolve `live/operations/<operation_id>.json`, shape-validating the id first."""
    if not _OPERATION_ID_RE.match(operation_id):
        raise KeyError(operation_id)
    return sandbox.live_operations_dir(root) / f"{operation_id}.json"


def _persist(record: LiveOperationRecord, root: Path) -> None:
    sandbox.ensure_live_dirs(root)
    path = _record_path(root, record.operation_id)
    path.write_text(record.model_dump_json(indent=2), encoding="utf-8")


def new_live_operation(
    *,
    tool: str,
    risk_class: RiskClass,
    params: dict[str, Any],
    transport: str | None = None,
    document: LiveDocumentRef | None = None,
    selection: list[str] | None = None,
    approval_token: str | None = None,
    settings: Settings | None = None,
) -> LiveOperationRecord:
    """Enforce the risk/approval gate, then mint + persist a `proposed` Live Operation Record.

    `enforce_risk_policy` runs FIRST: a HIGH-risk live op without `approval_token` raises
    `PolicyViolation` here, before any mutation is attempted. The resulting decision is stored in
    `policy_decision`. Raises `LiveRecordError` if no workspace root is configured to persist to.
    """
    s = settings if settings is not None else get_settings()
    policy_decision = enforce_risk_policy(risk_class, approval_token=approval_token)
    root = _first_root(s)
    now = _utc_iso()
    record = LiveOperationRecord(
        operation_id=_mint_operation_id(),
        tool=tool,
        risk_class=risk_class,
        params=params,
        transport=transport,
        document=_sanitize_document(document, s),
        selection=list(selection or []),
        policy_decision=policy_decision,
        created_at=now,
        updated_at=now,
    )
    _persist(record, root)
    return record


def update_live_operation(
    record: LiveOperationRecord,
    settings: Settings | None = None,
    **changes: Any,
) -> LiveOperationRecord:
    """Apply `changes`, bump `updated_at`, re-persist, and return the updated record."""
    s = settings if settings is not None else get_settings()
    root = _first_root(s)
    data = record.model_dump()
    data.update(changes)
    data["updated_at"] = _utc_iso()
    updated = LiveOperationRecord.model_validate(data)
    # Re-sanitize defensively in case a caller passed a fresh `document` in `changes` (sec.12).
    updated.document = _sanitize_document(updated.document, s)
    _persist(updated, root)
    return updated


def clear_live_operations(settings: Settings | None = None) -> int:
    """Delete all persisted Live Operation Records, returning how many were removed.

    Called on every live connect/disconnect boundary so a record from a prior session can never
    surface in the `live/operations` resource of a later one. Best-effort + idempotent: a missing
    directory or an unreadable file is ignored rather than raised — clearing must never break the
    session lifecycle. Degrades to a no-op when no workspace root is configured.
    """
    s = settings if settings is not None else get_settings()
    if not s.workspace_roots:
        return 0
    ops_dir = sandbox.live_operations_dir(s.workspace_roots[0])
    removed = 0
    try:
        files = list(ops_dir.glob("op_*.json"))
    except OSError:
        return 0
    for path in files:
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def get_live_operation(operation_id: str, settings: Settings | None = None) -> LiveOperationRecord:
    """Load a Live Operation Record from disk. Raises `KeyError` if unknown."""
    s = settings if settings is not None else get_settings()
    root = _first_root(s)
    path = _record_path(root, operation_id)
    if not path.is_file():
        raise KeyError(operation_id)
    return LiveOperationRecord.model_validate_json(path.read_text(encoding="utf-8"))


def list_live_operations(settings: Settings | None = None, limit: int = 50) -> LiveOperationLog:
    """Return recent Live Operation Records (newest first). Clean empty log when none exist."""
    s = settings if settings is not None else get_settings()
    if not s.workspace_roots:
        return LiveOperationLog(operations=[], count=0)
    ops_dir = sandbox.live_operations_dir(s.workspace_roots[0])
    records: list[LiveOperationRecord] = []
    try:
        files = sorted(ops_dir.glob("op_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return LiveOperationLog(operations=[], count=0)
    for path in files[: max(0, limit)]:
        try:
            records.append(
                LiveOperationRecord.model_validate_json(path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError):
            continue
    return LiveOperationLog(operations=records, count=len(records))
