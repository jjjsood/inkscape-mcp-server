"""Shared mutating-edit pipeline.

A single wrapper, :func:`apply_edit`, that every mutating tool (style / text / transform)
calls so each change is uniformly reversible and audited (ADR-004). For one edit it:

1. opens an Operation Record (`proposed`, risk policy enforced),
2. parses the working copy and CANONICAL-serializes it as the BEFORE content fingerprint,
3. runs the caller's in-memory DOM mutation and canonical-serializes again as the AFTER
   fingerprint,
4. if the two fingerprints are identical the edit is a genuine NO-OP — the record is retracted
   and the call returns `changed=False` with NOTHING written (see below),
5. otherwise renders a BEFORE preview from the still-pre-change working copy,
6. takes a pre-mutation snapshot (the revert target),
7. writes the mutated tree back over the working copy,
8. renders an AFTER preview from the now-post-change working copy,
9. links the snapshot and the before/after preview pair to the Operation Record and marks it
   `applied`.

No-op hygiene (the single source of truth for `changed`): the before/after content diff is
computed HERE, in the pipeline, by canonical-serializing the parsed tree both before and after
`mutate` runs (so reserialization noise — attribute order, whitespace — cancels and only a real
content change counts). When the content is unchanged the call returns `changed=False`, writes
NO snapshot and NO Operation Record (nothing happened → nothing to revert, nothing to log), and
carries a clear "no change" summary. Every mutating tool inherits this honest `changed` flag and
no-junk-snapshot behaviour for free — tools must NOT re-derive `changed` themselves.

Ordering matters: validation runs first, so a bad input transitions the record to `discarded`
and re-raises BEFORE any snapshot, preview, or write touches disk — never leaving an orphan
snapshot/preview. Rendering is best-effort: if Inkscape is unavailable the edit still applies
and the missing preview is recorded as `None`; the edit must never be lost just because a
preview could not be produced.

Reproducibility / naming: :func:`render_preview` writes a DETERMINISTIC file under
`artifacts/preview/preview-<descriptor>.png` (a second render overwrites the first), so each
render is immediately COPIED to an operation-specific name
(`artifacts/preview/op-<operation_id>-before.png` / `-after.png`) to keep both the before and
after frames for one operation.

SECURITY (sec.12): every filesystem path written here is derived from the registry entry's
resolved `root` / `workspace_dir` plus the server-minted opaque `operation_id` — never from
client input. The operation id is shape-validated by `new_operation`/`create_snapshot` before it
reaches any path, so a crafted id can never traverse out of the per-document artifact dir.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lxml import etree
from pydantic import BaseModel

from inkscape_mcp.document.inspect import DocumentNotFound
from inkscape_mcp.edit.dom import (
    EditError,
    TargetNotFound,
    load_working_tree,
    write_working_tree,
)
from inkscape_mcp.logging_setup import get_logger, log_file_io
from inkscape_mcp.operations import (
    OperationStatus,
    delete_operation,
    new_operation,
    update_operation,
)
from inkscape_mcp.registry import Registry
from inkscape_mcp.render.cli import (
    RenderError,
    RenderResult,
    render_preview,
)
from inkscape_mcp.snapshots import create_snapshot
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.limits import LimitExceeded
from inkscape_mcp.workspace.risk import RiskClass
from inkscape_mcp.workspace.subprocess_exec import ProcessError

_logger = get_logger("edit.pipeline")

#: Errors that mean "the preview could not be produced" but must NOT abort the edit. Rendering
#: is best-effort: any of these is caught, the corresponding preview is recorded as `None`, and
#: the mutation still applies (ADR-004 reversibility does not depend on a preview existing).
_RENDER_FAILURES = (RenderError, ProcessError, LimitExceeded)


class EditApplyError(Exception):
    """The edit could not be opened (unknown document or refused by the risk policy).

    Raised in place of the lower-level `KeyError` / `DocumentNotFound` from `new_operation` so
    the tool layer sees a single, stable, host-path-free failure for "could not start the edit".
    A `PolicyViolation` from the risk layer is allowed to propagate unchanged.
    """


# The mutate callback receives the parsed working tree, edits it IN MEMORY, and returns a short
# human summary string of what changed. It must raise EditError / TargetNotFound on bad input
# BEFORE any snapshot/preview/write happens.
MutateFn = Callable[[etree._ElementTree], str]


class EditResult(BaseModel):
    """Outcome of one :func:`apply_edit` call.

    When `changed` is True the edit produced a real content change: `operation_id` and
    `snapshot_id` identify the recorded Operation Record and its PRE-mutation snapshot (the revert
    target that restores the working copy to its pre-edit bytes), and `preview_before` /
    `preview_after` are WORKSPACE-RELATIVE POSIX paths to the operation-specific PNG frames (or
    `None` when that render was unavailable).

    When `changed` is False the edit was a genuine NO-OP — the document content is byte-identical
    before and after — so NOTHING was written: no snapshot, no Operation Record, no preview. In
    that case `operation_id` and `snapshot_id` are empty strings and the previews are `None`;
    `summary` explains that no change was made.
    """

    doc_id: str
    operation_id: str
    snapshot_id: str
    changed: bool
    summary: str | None = None
    preview_before: str | None = None
    preview_after: str | None = None


def _relative_to_workspace(workspace_dir: Path, out: Path) -> str:
    """Return `out` as a POSIX path relative to the document workspace dir (never absolute).

    Mirrors `render/cli.py::_relative_to_workspace`. The op-specific preview is always built
    under the per-document workspace dir, so a path outside it is a construction bug.
    """
    return out.relative_to(workspace_dir).as_posix()


def _render_op_preview(
    doc_id: str,
    *,
    root: Path,
    workspace_dir: Path,
    operation_id: str,
    phase: str,
) -> str | None:
    """Render the working copy and copy the deterministic PNG to an op-specific name.

    `phase` is `"before"` or `"after"`. `render_preview` writes a deterministic file that a
    later render would overwrite, so the produced PNG is COPIED to
    `artifacts/preview/op-<operation_id>-<phase>.png` to preserve both frames for this operation.
    Returns the workspace-relative POSIX path of the copy, or `None` if rendering was unavailable
    (best-effort: a render failure must never abort the edit).
    """
    try:
        result: RenderResult = render_preview(doc_id)
    except _RENDER_FAILURES as exc:
        _logger.warning(
            "preview render unavailable",
            extra={
                "event": "preview",
                "doc_id": doc_id,
                "operation_id": operation_id,
                "phase": phase,
                "error": type(exc).__name__,
            },
        )
        return None

    # `result.artifact_path` is relative to the workspace ROOT (one-location contract:
    # it carries the `.inkscape-mcp/documents/<doc_id>/...` base), so resolve it against `root`,
    # NOT `workspace_dir` — joining to the per-doc dir would double the base and miss the file.
    produced = root / result.artifact_path
    preview_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    dest = preview_dir / f"op-{operation_id}-{phase}.png"

    # Internal, registry-derived paths only (operation_id is server-minted) — never client input.
    shutil.copyfile(produced, dest)
    rel = _relative_to_workspace(workspace_dir, dest)
    log_file_io(
        _logger,
        action="op_preview",
        doc_id=doc_id,
        operation_id=operation_id,
        phase=phase,
        artifact=rel,
    )
    return rel


def _canonical_bytes(tree: etree._ElementTree) -> bytes:
    """Serialize the parsed tree to canonical bytes for the before/after content diff.

    Uses the EXACT serializer the working copy is written with
    (:func:`inkscape_mcp.edit.dom.write_working_tree` → `etree.tostring(... xml_declaration,
    UTF-8)`), so the fingerprint reflects the bytes that WOULD land on disk. Comparing the
    fingerprint taken before `mutate` against the one taken after isolates a REAL content change
    from incidental reserialization noise: a mutation that touches nothing produces identical
    bytes and is correctly reported as a no-op.
    """
    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8")


def apply_edit(
    doc_id: str,
    tool: str,
    params: dict[str, Any],
    mutate: MutateFn,
    registry: Registry | None = None,
    approval_token: str | None = None,
    risk_class: RiskClass = RiskClass.MEDIUM,
) -> EditResult:
    """Apply one in-memory DOM mutation as a fully recorded, reversible operation.

    `mutate` receives the parsed working tree, edits it IN MEMORY, and returns a short human
    summary of what changed. It MUST raise `EditError` / `TargetNotFound` on bad input before
    relying on any side effect; such a failure transitions the freshly-opened record to
    `discarded` and re-raises, with nothing written to disk.

    `risk_class` classes the operation for the policy gate (default MEDIUM for the safe-edit
    tools). HIGH-risk callers (e.g. the path tools) pass `RiskClass.HIGH` together with an
    `approval_token`; `new_operation` then refuses the op outright if the token is absent, so no
    snapshot/preview/write ever runs for an unapproved high-risk edit.

    No-op hygiene: the working copy is canonical-serialized before and after `mutate`. When the
    two fingerprints are identical the edit changed nothing — the freshly-`proposed` record is
    retracted, NO snapshot/preview/write is produced, and an `EditResult` with `changed=False`,
    empty `operation_id`/`snapshot_id`, and a "no change" summary is returned. The pipeline is the
    single source of truth for `changed`; callers must not re-derive it.

    On a real change the working copy is updated, a pre-mutation snapshot is linked as the revert
    target, a before/after preview pair is attached to the Operation Record (`applied`), and
    `changed=True` is returned. Rendering is best-effort — a missing preview yields `None` and
    never aborts the edit.

    Raises `EditApplyError` if the document is unknown, `PolicyViolation` if the operation is
    refused by the risk policy, and re-raises `EditError` / `TargetNotFound` from `mutate`.
    """
    # 1. Open the Operation Record (enforces policy, persists `proposed`). Map a missing
    #    document to a clear, stable failure; let PolicyViolation propagate unchanged.
    try:
        record = new_operation(
            doc_id,
            tool,
            risk_class,
            params,
            registry,
            approval_token,
        )
    except (KeyError, DocumentNotFound) as exc:
        raise EditApplyError("document id not found") from exc

    # 2. Parse the working copy and fingerprint it BEFORE the mutation (canonical bytes).
    entry, tree = load_working_tree(doc_id, registry)
    root = Path(entry.root)
    workspace_dir = Path(entry.workspace_dir)
    before_bytes = _canonical_bytes(tree)

    # 3. Run the mutation IN MEMORY. A bad input discards the record and re-raises before any
    #    snapshot / preview / write has touched disk.
    try:
        summary = mutate(tree)
    except (EditError, TargetNotFound):
        # Mark the record discarded, but never let a failure here mask the real mutation error
        # (e.g. the operations dir vanished): log and still re-raise the original cause.
        try:
            update_operation(record, registry, status=OperationStatus.DISCARDED)
        except Exception:
            _logger.exception("failed to mark operation discarded", extra={"doc_id": doc_id})
        raise

    # 3b. NO-OP HYGIENE (the single source of truth for `changed`): if the mutation left the
    #     canonical content byte-identical, nothing actually changed. Retract the freshly-proposed
    #     record and return `changed=False` WITHOUT writing a snapshot, a preview, or the working
    #     copy — a no-op has nothing to revert and nothing worth logging.
    after_bytes = _canonical_bytes(tree)
    if after_bytes == before_bytes:
        try:
            delete_operation(record, registry)
        except Exception:
            # Never let cleanup failure mask success; the record is at worst an inert `proposed`.
            _logger.exception("failed to retract no-op operation", extra={"doc_id": doc_id})
        return EditResult(
            doc_id=doc_id,
            operation_id="",
            snapshot_id="",
            changed=False,
            summary=f"no change: {summary}",
            preview_before=None,
            preview_after=None,
        )

    # 4. BEFORE preview: the on-disk working copy is still pre-change. Best-effort.
    preview_before = _render_op_preview(
        doc_id,
        root=root,
        workspace_dir=workspace_dir,
        operation_id=record.operation_id,
        phase="before",
    )

    # 5. Pre-mutation snapshot: on-disk is still pre-change, so this is the correct revert target.
    pre = create_snapshot(
        doc_id,
        label=f"pre-{tool}",
        operation_id=record.operation_id,
        registry=registry,
    )

    # 6. Persist the mutation: the working copy is now post-change.
    write_working_tree(entry, tree)

    # 7. AFTER preview: the on-disk working copy now reflects the mutation. Best-effort.
    preview_after = _render_op_preview(
        doc_id,
        root=root,
        workspace_dir=workspace_dir,
        operation_id=record.operation_id,
        phase="after",
    )

    # 8. Link the snapshot + the previews that succeeded, and mark the record `applied`.
    previews = {
        phase: path
        for phase, path in (("before", preview_before), ("after", preview_after))
        if path is not None
    }
    update_operation(
        record,
        registry,
        snapshot_id=pre.snapshot_id,
        previews=previews,
        status=OperationStatus.APPLIED,
    )

    # 9. Return the typed result.
    return EditResult(
        doc_id=doc_id,
        operation_id=record.operation_id,
        snapshot_id=pre.snapshot_id,
        changed=True,
        summary=summary,
        preview_before=preview_before,
        preview_after=preview_after,
    )


__all__ = ["EditApplyError", "EditResult", "MutateFn", "apply_edit"]
