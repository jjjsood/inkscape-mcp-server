"""Typed DOM-edit BATCH tool: ``apply_edits`` — N typed edits, one atomic operation.

Thin MCP layer over the batch engine in :mod:`inkscape_mcp.edit.batch`. It submits an ordered list
of TYPED edits (a discriminated union over the existing direct-DOM ops — NOT free text, NOT a
raw-action path, ADR-002/003) and applies them through the SINGLE existing edit kernel
(:func:`inkscape_mcp.edit.pipeline.apply_edit`), so the whole batch is:

* **validated-all-first** — every member's schema + values are validated before any mutation, so one
  malformed edit fails the whole batch with the document byte-identical (mirrors
  ``validate_action_chain``);
* **atomic** — members run in order against one in-memory tree; any failure rolls the batch back
  before a byte lands on disk (all-or-nothing);
* **reversible** — exactly ONE snapshot + ONE Operation Record per batch, so a single
  ``restore_snapshot`` reverts the entire batch (ADR-004);
* **risk = max over members** — a ``high`` member (e.g. a ``delete_object`` edit) escalates the
  whole batch to HIGH and forces the same per-op ``approval_token`` gate that member would demand.

This closes the round-trip tax the small-typed-tool model pays versus a single ``execute_code`` call
WITHOUT giving up typing, validation, or reversibility — the only real weakness the Penpot survey
found, removed while keeping the advantages Penpot lacks (atomic + typed + reversible).

Client-facing errors map to `ToolError` with stable, host-path-free messages (sec.12), exactly as
the single-edit tools do.
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from inkscape_mcp.document.inspect import DocumentNotFound, InspectionError
from inkscape_mcp.edit.batch import BatchTooLarge, InvalidBatchEdit, TypedEdit, build_batch
from inkscape_mcp.edit.dom import EditError, TargetNotFound
from inkscape_mcp.edit.pipeline import EditApplyError, EditResult, apply_edit
from inkscape_mcp.edit.transform import ContentBBoxError
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.server import mcp
from inkscape_mcp.workspace.risk import PolicyViolation

_logger = get_logger("tools.batch")


class BatchEditResult(EditResult):
    """An :class:`EditResult` for the whole batch, extended with the count and effective risk.

    All `EditResult` fields describe the ONE snapshot + Operation Record the batch produced
    (`operation_id`, `snapshot_id`, `changed`, the before/after preview pair — the batch lands on
    the working copy only, reversible via `restore_snapshot`). `edit_count` is the number of edits
    submitted; `risk_class` is the batch's effective risk (the MAX over its members). On a genuine
    no-op (every member changed nothing) `changed` is False and `operation_id` / `snapshot_id` are
    empty, exactly as for a single no-op edit.
    """

    edit_count: int
    risk_class: str


@mcp.tool
def apply_edits(
    doc_id: str,
    edits: list[TypedEdit],
    approval_token: str | None = None,
) -> BatchEditResult:
    """Apply an ordered list of typed DOM edits to a document as ONE atomic, reversible operation.

    When to use: making SEVERAL edits to one document in a single call (draw + style + arrange,
    re-theme + rename, …) instead of N separate tool round-trips. Each member is the SAME typed
    edit the dedicated tool exposes — `apply_edits` adds atomicity + one snapshot, not new
    authority. For a single edit, call the dedicated tool (`set_fill`, `move_object`, …); for path
    geometry or cross-document composition use those tools directly (they are NOT batchable).

    Key params: `edits` is a non-empty, ordered list (max 64) of typed edits, each tagged by an `op`
    field that selects its schema — e.g. `{"op": "create_rect", "x": 0, "y": 0, "width": 100,
    "height": 60, "fill": "#3366cc"}`, `{"op": "set_fill", "object_ids": ["logo"], "color": "red"}`,
    `{"op": "move_object", "object_id": "logo", "dx": 10, "dy": 0}`. Supported ops mirror the typed
    DOM tools: `set_fill` / `set_stroke` / `set_opacity` / `replace_color` / `apply_palette` /
    `replace_text` / `set_font` / `duplicate_object` / `rename_object` / `delete_object` (high) /
    `move_object` / `scale_object` / `rotate_object` / `resize_canvas` / `normalize_viewbox` /
    `tile` / `create_rect` / `create_circle` / `create_ellipse` / `create_line` / `create_polygon` /
    `create_polyline` / `create_path` / `create_text` / `create_group` / `group_objects` /
    `reparent_object` / `create_use` / `add_linear_gradient` / `add_radial_gradient`. Validation is
    two-phase: ALL members are validated before any mutation (one bad edit leaves the document
    byte-identical), then applied in order with all-or-nothing rollback on any failure. If ANY
    member is high-risk (a `delete_object` edit) the WHOLE batch is HIGH and requires a non-empty
    `approval_token`; otherwise it is medium.

    Render and look before you trust the edit: a batch changes several things at once, so call
    `render_preview` (or `live_render_view` in live mode) afterwards and inspect the result before
    relying on it — and `restore_snapshot(doc_id, snapshot_id)` reverts the whole batch in one step.

    Return shape: `BatchEditResult` — the pipeline fields for the single batch operation
    (`operation_id`, `snapshot_id`, `changed`, before/after preview; reversible) PLUS `edit_count`
    and the effective `risk_class`.

    Example: `apply_edits(doc_id, [{"op": "create_rect", "x": 0, "y": 0, "width": 100, "height": 60,
    "fill": "#eee", "object_id": "bg"}, {"op": "create_text", "x": 10, "y": 30, "text": "Hi"}])`

    Risk class: medium (effective risk is the MAX over members; a `delete_object` member escalates
    the batch to high and requires `approval_token`). Reversible via the single pre-batch snapshot.
    """
    try:
        mutate, risk, op_names = build_batch(edits)
        result = apply_edit(
            doc_id,
            "apply_edits",
            {"edit_count": len(op_names), "ops": op_names},
            mutate,
            approval_token=approval_token,
            risk_class=risk,
        )
    except (BatchTooLarge, InvalidBatchEdit) as exc:
        raise ToolError(str(exc)) from exc
    except (EditApplyError, DocumentNotFound, KeyError) as exc:
        raise ToolError("document id not found") from exc
    except TargetNotFound as exc:
        raise ToolError("object id not found in document") from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc
    except PolicyViolation as exc:
        raise ToolError(str(exc)) from exc
    except ContentBBoxError as exc:  # pragma: no cover - no batch op queries the engine bbox
        raise ToolError(str(exc)) from exc
    except EditError as exc:
        raise ToolError(str(exc)) from exc

    log_tool_call(
        _logger,
        tool="apply_edits",
        doc_id=doc_id,
        operation_id=result.operation_id,
        count=len(edits),
    )
    return BatchEditResult(
        **result.model_dump(),
        edit_count=len(edits),
        risk_class=risk.value,
    )
