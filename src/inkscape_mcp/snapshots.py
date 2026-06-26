"""Snapshot engine + restore (E1-07 / ADR-004 reversibility).

Pure functions (no MCP decorators) that implement the reversibility primitives of the workspace
model: snapshot naming/index and the ADR-004 restore chain. A snapshot is a full
byte-for-byte copy of the current `working/document.svg`; restore copies a snapshot file back
over the working copy and records the restore as its own Operation Record so an undo is itself
auditable and reversible. The original source file is NEVER touched by any function here.

Path safety (sec.12): every path is built from the registry's resolved `root` + the internal
opaque `doc_id` — never from a client-supplied path. The only client input that reaches this
module is `doc_id`, an optional `label`, and (for restore) a `snapshot_id`; `snapshot_id` is
resolved by looking it up in `index.json` (the authoritative manifest), never by interpolating
it into a path, so a crafted id (e.g. `../../etc/passwd`) cannot select an out-of-dir file.

Filename scheme (§6): `snapshots/<seq>-<id-token>-<utc-timestamp>.svg`, where `<seq>` is a
zero-padded monotonic counter (`0001`, `0002`, …) derived from the existing index, `<id-token>`
is the triggering Operation Record id when one is supplied, else the minted snapshot id token
(standalone `create_snapshot` calls have no operation id — documented deviation), and
`<utc-timestamp>` is `YYYYMMDDTHHMMSSZ`. `index.json` is the authoritative ordered manifest;
seq `0000` is conceptually the `original.svg` baseline and is never minted here.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from inkscape_mcp.logging_setup import get_logger, log_file_io
from inkscape_mcp.operations import OperationStatus, new_operation, update_operation
from inkscape_mcp.registry import Registry, get_registry
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.limits import check_input_size
from inkscape_mcp.workspace.risk import RiskClass

_logger = get_logger("snapshots")

#: Zero-padding width for the monotonic `<seq>` counter (`0001`, …).
_SEQ_WIDTH = 4

#: Minted snapshot-id shape (`snap_` + 8 hex chars). Used to validate any caller-supplied
#: snapshot_id BEFORE it is matched against the index, so a crafted id is rejected cheaply.
_SNAPSHOT_ID_RE = re.compile(r"^snap_[0-9a-f]{8}$")

#: Minted operation-id shape (`op_` + 8 hex chars). `create_snapshot` puts the operation id
#: into the snapshot filename, so it must be shape-checked before it touches a path even though
#: every in-tree caller passes a registry-minted id (defense-in-depth against a bad public call).
_OPERATION_ID_RE = re.compile(r"^op_[0-9a-f]{8}$")


class SnapshotNotFound(Exception):
    """No snapshot with the given id exists in the document's index.

    Public message is stable and carries no host path; the tool layer maps it to a
    `ToolError`.
    """


class SnapshotInfo(BaseModel):
    """One entry in a document's snapshot index (§6 manifest fields).

    `file` is the snapshot's BASENAME inside the per-document `snapshots/` dir, never an
    absolute host path, so it can be returned to a client without leaking the layout.
    """

    snapshot_id: str
    seq: int
    file: str
    created_at: str
    label: str | None = None
    operation_id: str | None = None
    size_bytes: int


class SnapshotList(BaseModel):
    """Ordered snapshot manifest for one document (clean tool outputSchema wrapper)."""

    doc_id: str
    snapshots: list[SnapshotInfo] = Field(default_factory=list)


class RestoreResult(BaseModel):
    """Outcome of `restore_snapshot`: the reversibility chain links (§7) + recovery assertion.

    `restored_sha256` and `restored_size_bytes` are the SHA-256 hex digest and byte length of the
    working copy AS IT NOW STANDS after the restore, so an agent can assert recovery succeeded
    without reading the working file off disk (E11-10(d) / S11).
    """

    doc_id: str
    restored_from: str
    operation_id: str
    pre_restore_snapshot_id: str
    restored_sha256: str
    restored_size_bytes: int


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _utc_timestamp() -> str:
    """`YYYYMMDDTHHMMSSZ` (UTC) for the deterministic snapshot filename (§6)."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _mint_snapshot_id() -> str:
    return f"snap_{secrets.token_hex(4)}"


def _resolve_root(doc_id: str, registry: Registry) -> Path:
    """Resolve the document's owning root via the registry. Raises `KeyError` if unknown."""
    entry = registry.get(doc_id)
    return Path(entry.root)


def _read_index(root: Path, doc_id: str) -> list[SnapshotInfo]:
    """Read the ordered snapshot index, or return `[]` if none exists yet.

    The index is authoritative: callers resolve a snapshot by matching its `snapshot_id`
    against these entries, never by trusting a client-supplied path.
    """
    index_path = sandbox.snapshots_index(root, doc_id)
    if not index_path.is_file():
        return []
    raw = json.loads(index_path.read_text(encoding="utf-8"))
    entries = raw.get("snapshots", []) if isinstance(raw, dict) else []
    return [SnapshotInfo.model_validate(item) for item in entries]


def _write_index(root: Path, doc_id: str, entries: list[SnapshotInfo]) -> None:
    """Write the ordered index atomically (write-temp + os.replace within the same dir)."""
    index_path = sandbox.snapshots_index(root, doc_id)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"snapshots": [e.model_dump() for e in entries]}
    tmp = index_path.with_name(f"{index_path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(index_path)


def _next_seq(entries: list[SnapshotInfo]) -> int:
    """Next monotonic seq = max existing + 1, starting at 1 (seq 0 is the baseline)."""
    if not entries:
        return 1
    return max(e.seq for e in entries) + 1


def create_snapshot(
    doc_id: str,
    label: str | None = None,
    operation_id: str | None = None,
    registry: Registry | None = None,
) -> SnapshotInfo:
    """Copy the current `working/document.svg` into the snapshots dir and index it.

    Mints a `snap_<hex>` id, derives the next zero-padded monotonic `<seq>` from the existing
    index, and writes `snapshots/<seq>-<id-token>-<utc-timestamp>.svg`. The `<id-token>` is
    `operation_id` when supplied (so a pre-mutation snapshot links to its triggering record by
    name) else the minted snapshot id token — standalone `create_snapshot` calls have no
    operation id, the one documented deviation from §6's `<operation-id>` slot. Appends the
    entry to the authoritative `index.json` and returns its `SnapshotInfo`.

    The original source file is never read or written here; only the working copy is copied.
    """
    reg = registry if registry is not None else get_registry()
    root = _resolve_root(doc_id, reg)

    sandbox.ensure_doc_dirs(root, doc_id)
    working = sandbox.working_copy(root, doc_id)

    if operation_id is not None and not _OPERATION_ID_RE.match(operation_id):
        raise SnapshotNotFound("invalid operation id")

    snapshot_id = _mint_snapshot_id()
    entries = _read_index(root, doc_id)
    seq = _next_seq(entries)

    id_token = operation_id if operation_id is not None else snapshot_id
    filename = f"{seq:0{_SEQ_WIDTH}d}-{id_token}-{_utc_timestamp()}.svg"
    target = sandbox.snapshots_dir(root, doc_id) / filename

    # Copy the working copy bytes (internal, registry-derived paths — never client input).
    shutil.copyfile(working, target)
    size_bytes = target.stat().st_size

    info = SnapshotInfo(
        snapshot_id=snapshot_id,
        seq=seq,
        file=filename,
        created_at=_utc_iso(),
        label=label,
        operation_id=operation_id,
        size_bytes=size_bytes,
    )
    entries.append(info)
    _write_index(root, doc_id, entries)

    log_file_io(
        _logger,
        action="create_snapshot",
        doc_id=doc_id,
        snapshot_id=snapshot_id,
        seq=seq,
        operation_id=operation_id,
    )
    return info


def list_snapshots(doc_id: str, registry: Registry | None = None) -> list[SnapshotInfo]:
    """Return the document's ordered snapshot index (empty list if none yet).

    Raises `KeyError` if `doc_id` is unknown (propagated from the registry).
    """
    reg = registry if registry is not None else get_registry()
    root = _resolve_root(doc_id, reg)
    return _read_index(root, doc_id)


def _find_snapshot(entries: list[SnapshotInfo], snapshot_id: str) -> SnapshotInfo:
    """Look a snapshot up in the index by id, validating its minted shape first.

    The shape check rejects a crafted id (e.g. `../../etc/passwd`) before any lookup; the
    index match guarantees the resolved `file` is one this server itself wrote, so a restore
    can never select a file outside the per-document snapshots dir. Raises `SnapshotNotFound`.
    """
    if not _SNAPSHOT_ID_RE.match(snapshot_id):
        raise SnapshotNotFound("snapshot not found")
    for entry in entries:
        if entry.snapshot_id == snapshot_id:
            return entry
    raise SnapshotNotFound("snapshot not found")


def restore_snapshot(
    doc_id: str,
    snapshot_id: str,
    registry: Registry | None = None,
) -> RestoreResult:
    """Revert the working copy to a snapshot, recording the restore as its own operation.

    The reversibility chain (ADR-004 / §7):
    1. Open a `medium`-risk Operation Record (`restore_snapshot`) — medium ops must create a
       record; `new_operation` enforces the risk policy and persists it as `proposed`.
    2. Take a PRE-RESTORE snapshot of the current working copy and link it to the record, so
       the restore is itself reversible (you can undo the undo).
    3. Resolve the target snapshot through the index (shape-validated id + index match — never
       a path interpolation), then copy its file back over `working/document.svg` ONLY.
    4. Transition the record to `applied` with the pre-restore `snapshot_id` linked.

    Raises `KeyError` for an unknown `doc_id`, `SnapshotNotFound` for an absent/crafted
    `snapshot_id`. The original source file is never touched; only the working copy is written.
    """
    reg = registry if registry is not None else get_registry()
    root = _resolve_root(doc_id, reg)

    # Resolve the target FIRST so an unknown/crafted id fails before any record/snapshot is
    # written (nothing is created, nothing outside the sandbox is touched).
    entries = _read_index(root, doc_id)
    target = _find_snapshot(entries, snapshot_id)

    record = new_operation(
        doc_id,
        tool="restore_snapshot",
        risk_class=RiskClass.MEDIUM,
        params={"snapshot_id": snapshot_id},
        registry=reg,
    )

    # Pre-restore snapshot of the CURRENT working copy → makes this restore reversible.
    pre = create_snapshot(
        doc_id,
        label=f"pre-restore before {snapshot_id}",
        operation_id=record.operation_id,
        registry=reg,
    )

    # Copy the target snapshot's file back over the working copy ONLY (registry-derived paths).
    # `target.file` is a manifest-stored basename; assert it has no path component so a tampered
    # index entry (e.g. "../../working/document.svg") cannot redirect the restore source out of
    # the snapshots dir. Size-check it for parity with the open-document flow.
    if Path(target.file).name != target.file:
        raise SnapshotNotFound("snapshot not found")
    source = sandbox.snapshots_dir(root, doc_id) / target.file
    check_input_size(source)
    working = sandbox.working_copy(root, doc_id)
    shutil.copyfile(source, working)

    # Hash + size the restored working copy so a caller can assert recovery without disk access
    # (E11-10(d) / S11). Read from the just-written working copy — the authoritative restored state.
    restored_bytes = working.read_bytes()
    restored_sha256 = hashlib.sha256(restored_bytes).hexdigest()
    restored_size_bytes = len(restored_bytes)

    update_operation(
        record,
        registry=reg,
        snapshot_id=pre.snapshot_id,
        status=OperationStatus.APPLIED,
    )

    log_file_io(
        _logger,
        action="restore_snapshot",
        doc_id=doc_id,
        restored_from=snapshot_id,
        operation_id=record.operation_id,
        pre_restore_snapshot_id=pre.snapshot_id,
    )
    return RestoreResult(
        doc_id=doc_id,
        restored_from=snapshot_id,
        operation_id=record.operation_id,
        pre_restore_snapshot_id=pre.snapshot_id,
        restored_sha256=restored_sha256,
        restored_size_bytes=restored_size_bytes,
    )
