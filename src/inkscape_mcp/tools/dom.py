"""DOM structural-edit tools (E16-08): `delete_object` — HIGH-risk, reversible.

Thin MCP layer over the direct-DOM engine `make_delete_objects` in
:mod:`inkscape_mcp.edit.text_object`. The edit routes through the shared, reversible edit pipeline
(:func:`inkscape_mcp.edit.pipeline.apply_edit`), so it is uniformly snapshotted, recorded as an
Operation Record, and linked to a before/after preview (ADR-004) — exactly like the E2 mutating
tools. Editing is direct lxml on the DOM only (ADR-005); the tool is small and typed (no
portmanteau) per ADR-002.

Risk: deleting elements is HIGH risk (architecture risk classes: delete = high). The pipeline is
passed ``RiskClass.HIGH`` together with the caller's ``approval_token``; the policy gate refuses
the op outright when the token is absent, so no snapshot / preview / write ever runs for an
unapproved delete (sec.12). The deletion is fully reversible via the pre-op snapshot
(`restore_snapshot`).

No-op hygiene (E10-05 / E11-13): when NONE of the supplied ids exist the engine leaves the tree
byte-identical, so the pipeline reports ``changed=False`` and writes NO snapshot / Operation Record;
the tool surfaces ``affected_ids=[]`` for that case. This layer maps engine / pipeline / policy
exceptions to `ToolError` with stable, host-path-free messages (sec.12).
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from inkscape_mcp.document.inspect import DocumentNotFound, InspectionError
from inkscape_mcp.edit.dom import EditError, TargetNotFound, all_ids, load_working_tree
from inkscape_mcp.edit.pipeline import EditApplyError, EditResult, apply_edit
from inkscape_mcp.edit.text_object import make_delete_objects
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.server import mcp
from inkscape_mcp.workspace.risk import PolicyViolation, RiskClass

_logger = get_logger("tools.dom")


class DeleteResult(EditResult):
    """`EditResult` for `delete_object`, additively extended with the removed ids (E16-08).

    All `EditResult` fields are preserved. `affected_ids` lists the object ids that were ACTUALLY
    removed (ids that did not exist in the document are silently skipped, so they never appear
    here). On a genuine no-op (none of the supplied ids existed) `changed` is False, `operation_id`
    / `snapshot_id` are empty, and `affected_ids` is `[]`.
    """

    affected_ids: list[str]


def _present_ids(doc_id: str, object_ids: list[str]) -> list[str]:
    """The subset of `object_ids` that exist in the document's working copy (order preserved).

    Computed from the PRE-edit working tree so the tool can report exactly which ids the engine will
    remove. Raises the same engine exceptions `apply_edit` does for an unknown / unparseable
    document, which the caller maps to a stable `ToolError`.
    """
    _entry, tree = load_working_tree(doc_id)
    existing = all_ids(tree.getroot())
    return [oid for oid in object_ids if oid in existing]


@mcp.tool
def delete_object(
    doc_id: str,
    object_ids: list[str],
    approval_token: str | None = None,
) -> DeleteResult:
    """Delete objects by id from a document in ONE reversible, snapshot-backed operation.

    When to use: dropping one or more existing elements (e.g. stray seed paths) without a `Read` +
    `set_document_svg` full-document rebuild. Get ids from `find_objects` / `inspect_document`. To
    MOVE an object into another group use `reparent_object`; to rename rather than remove use
    `rename_object`.

    Key params: `object_ids` is a non-empty list of ids to remove; an id that is not present is
    silently skipped (deleting an already-absent object is a successful no-op, not an error). The
    document root cannot be deleted. Because deletion is HIGH risk, a real removal requires a
    non-empty `approval_token` (minted out of band, bound to this one operation); without it the
    policy gate refuses the op and nothing is written.

    Return shape: `DeleteResult` — all `EditResult` fields (`operation_id`, `snapshot_id`,
    `changed`, before/after preview; the edit lands on the working copy only, reversible via
    `restore_snapshot`) PLUS `affected_ids`, the ids that were actually removed. When NONE of the
    ids existed the call is a genuine no-op: `changed=False`, empty `operation_id`/`snapshot_id`,
    and `affected_ids=[]` (no snapshot or Operation Record written).

    Example: `delete_object(doc_id, ["seed1", "seed2"], approval_token="…")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: high (delete; approval-gated, reversible via pre-op snapshot — original untouched).
    """
    if not object_ids:
        raise ToolError("delete_object requires at least one object id")

    try:
        affected = _present_ids(doc_id, object_ids)
        result = apply_edit(
            doc_id,
            "delete_object",
            {"object_ids": object_ids},
            make_delete_objects(object_ids),
            approval_token=approval_token,
            risk_class=RiskClass.HIGH,
        )
    except (EditApplyError, DocumentNotFound, KeyError) as exc:
        raise ToolError("document id not found") from exc
    except TargetNotFound as exc:  # pragma: no cover - delete skips missing ids
        raise ToolError("object id not found in document") from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc
    except PolicyViolation as exc:
        raise ToolError(str(exc)) from exc
    except EditError as exc:
        raise ToolError(str(exc)) from exc

    # On a genuine no-op the pipeline wrote nothing; report no affected ids (matches changed=False).
    affected_out = affected if result.changed else []
    log_tool_call(
        _logger,
        tool="delete_object",
        doc_id=doc_id,
        count=len(affected_out),
        operation_id=result.operation_id,
    )
    return DeleteResult(**result.model_dump(), affected_ids=affected_out)
