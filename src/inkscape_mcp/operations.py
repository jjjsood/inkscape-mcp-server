"""Operation Record scaffold (ADR-004).

Every mutating tool call creates an Operation Record linking params → policy decision →
invocation → snapshot → artifacts → logs, persisted as append-only JSON under the document's
`operations/` dir and referenceable by id. `snapshot_id` links to the snapshot taken
before the mutation; `previews` links the before/after PNG preview pair produced for the change
(keyed `"before"` / `"after"`, workspace-relative paths). Records move proposed → applied
(or discarded / reverted).
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from inkscape_mcp.registry import Registry, get_registry
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.risk import RiskClass, enforce_risk_policy

#: Minted operation-id shape (`op_` + 8 hex chars). Enforced before any path construction
#: so a caller-supplied id can never traverse out of the per-document `operations/` dir.
_OPERATION_ID_RE = re.compile(r"^op_[0-9a-f]{8}$")


class OperationStatus(StrEnum):
    """Lifecycle status of an Operation Record (architecture §3.3)."""

    PROPOSED = "proposed"
    APPLIED = "applied"
    DISCARDED = "discarded"
    REVERTED = "reverted"


class OperationRecord(BaseModel):
    """Append-only record of one mutating operation (ADR-004)."""

    operation_id: str
    doc_id: str
    tool: str
    risk_class: RiskClass
    params: dict[str, Any]
    policy_decision: dict[str, Any] = Field(default_factory=dict)
    invocation: dict[str, Any] | None = None
    snapshot_id: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    previews: dict[str, str] = Field(default_factory=dict)
    logs: list[str] = Field(default_factory=list)
    status: OperationStatus = OperationStatus.PROPOSED
    created_at: str
    updated_at: str


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _mint_operation_id() -> str:
    return f"op_{secrets.token_hex(4)}"


def _operation_path(doc_id: str, operation_id: str, registry: Registry) -> Path:
    """Resolve `operations/<operation_id>.json` for a document via the registry.

    `operation_id` is validated against the minted `op_<8hex>` shape before it is used to
    build a path, so a caller-supplied id can never traverse out of the `operations/` dir.
    """
    if not _OPERATION_ID_RE.match(operation_id):
        raise KeyError(operation_id)
    entry = registry.get(doc_id)
    root = Path(entry.root)
    return sandbox.operations_dir(root, doc_id) / f"{operation_id}.json"


def _persist(record: OperationRecord, registry: Registry) -> None:
    path = _operation_path(record.doc_id, record.operation_id, registry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(indent=2), encoding="utf-8")


def new_operation(
    doc_id: str,
    tool: str,
    risk_class: RiskClass,
    params: dict[str, Any],
    registry: Registry | None = None,
    approval_token: str | None = None,
) -> OperationRecord:
    """Mint an Operation Record, persist it as `proposed`, and return it.

    The risk policy is enforced first (`enforce_risk_policy`): `restricted` is always
    refused and `high` requires an `approval_token`; the resulting decision is recorded in
    `policy_decision`. Resolves the document via the registry to find its root and writes
    `operations/<operation_id>.json`. Raises `KeyError` if `doc_id` is unknown and
    `PolicyViolation` if the operation is not permitted.
    """
    reg = registry if registry is not None else get_registry()
    policy_decision = enforce_risk_policy(risk_class, approval_token=approval_token)
    now = _utc_iso()
    record = OperationRecord(
        operation_id=_mint_operation_id(),
        doc_id=doc_id,
        tool=tool,
        risk_class=risk_class,
        params=params,
        policy_decision=policy_decision,
        created_at=now,
        updated_at=now,
    )
    _persist(record, reg)
    return record


def update_operation(
    record: OperationRecord,
    registry: Registry | None = None,
    **changes: Any,
) -> OperationRecord:
    """Apply `changes`, bump `updated_at`, re-persist, and return the updated record.

    Used to link `snapshot_id` / `artifacts` and to transition `status`.
    """
    reg = registry if registry is not None else get_registry()
    data = record.model_dump()
    data.update(changes)
    data["updated_at"] = _utc_iso()
    updated = OperationRecord.model_validate(data)
    _persist(updated, reg)
    return updated


def get_operation(
    doc_id: str,
    operation_id: str,
    registry: Registry | None = None,
) -> OperationRecord:
    """Load an Operation Record from disk. Raises `KeyError` for an unknown document/op."""
    reg = registry if registry is not None else get_registry()
    path = _operation_path(doc_id, operation_id, reg)
    if not path.is_file():
        raise KeyError(operation_id)
    return OperationRecord.model_validate_json(path.read_text(encoding="utf-8"))


def delete_operation(
    record: OperationRecord,
    registry: Registry | None = None,
) -> None:
    """Remove a persisted Operation Record from disk (idempotent if already gone).

    Used by the edit pipeline to retract a freshly-`proposed` record when the caller's mutation
    turns out to be a genuine NO-OP (the document content is byte-identical before and after): a
    no-op changed nothing, so there is nothing to revert and nothing worth logging — the record
    must not linger and clutter the audit trail. The `operation_id` is shape-validated by
    `_operation_path` before any path is built, so a crafted id can never escape the dir.
    """
    reg = registry if registry is not None else get_registry()
    path = _operation_path(record.doc_id, record.operation_id, reg)
    path.unlink(missing_ok=True)
