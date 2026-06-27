"""Batch-export tool: `export_batch`.

Thin MCP layer over the batch engine (`inkscape_mcp.render.batch`), which composes the CLI
render/export engine. Inkscape engine per ADR-005. Risk class **low**: artifact-only, no new
authority over the single-export tools — it adds only the batch bounds (item cap + total byte
budget + dry-run default).

Following ADR-002 the input is a TYPED list of `ExportSpec` (NOT a string / portmanteau).
`dry_run` defaults to True: the call validates every spec and reports the planned exports +
projected sizes, writing nothing. A real run (`dry_run=False`) refuses cleanly if the projected
total exceeds the byte budget, then exports each spec through the engine.

Client-facing errors are raised as `ToolError` with stable, host-path-free messages (sec.12):
unknown document id -> "document id not found"; a malformed / oversized / over-budget batch -> the
validation message (built from typed parameters, no host path); a render/limit/process failure ->
"render/export failed". Returned artifact paths follow ONE LOCATION CONTRACT:
`artifact_path` and `workspace_relative_path` carry the SAME value, always relative to the WORKSPACE
ROOT, openable by a single join to the root — never an absolute host path.
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from inkscape_mcp.edit.collection import ConsistencyVerdict
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.render.batch import (
    BatchError,
    BatchResult,
    DocumentNotFound,
    ExportSpec,
    InspectionError,
)
from inkscape_mcp.render.batch import (
    export_batch as _export_batch,
)
from inkscape_mcp.render.cli import RenderError
from inkscape_mcp.server import mcp
from inkscape_mcp.tools.collection_set import build_set_verdict, require_unique_doc_ids
from inkscape_mcp.workspace.limits import LimitExceeded
from inkscape_mcp.workspace.paths import SandboxViolation
from inkscape_mcp.workspace.subprocess_exec import ProcessError

_logger = get_logger("tools.export_batch")


def _map_failure(exc: Exception) -> ToolError:
    """Map an engine exception to a stable, host-path-free `ToolError` (sec.12)."""
    if isinstance(exc, (KeyError, DocumentNotFound)):
        return ToolError("document id not found")
    if isinstance(exc, InspectionError):
        return ToolError("document could not be parsed safely")
    if isinstance(exc, SandboxViolation):
        # Already a SAFE public message (no host path) — e.g. "path rejected: outside workspace".
        return ToolError(str(exc))
    if isinstance(exc, BatchError):
        # Validation/bounds message, built from typed parameters — already safe (no host path).
        return ToolError(str(exc))
    if isinstance(exc, LimitExceeded):
        return ToolError("export exceeds the configured size or dimension limit")
    if isinstance(exc, ProcessError):
        # CAPABILITY-ABSENT: the Inkscape engine could not be launched on this runtime.
        # Name the discovery tool so the agent can inspect support rather than retry blindly.
        return ToolError(
            "render/export failed: the Inkscape engine is unavailable on this runtime; "
            "call list_capabilities to see what this runtime supports"
        )
    if isinstance(exc, RenderError):
        return ToolError("render/export failed")
    return ToolError("render/export failed")


@mcp.tool
def export_batch(
    doc_id: str,
    specs: list[ExportSpec],
    dry_run: bool = True,
    byte_budget: int | None = None,
    out_dir: str | None = None,
    name_prefix: str | None = None,
) -> BatchResult:
    """Run a bounded batch of typed export specs in one call (dry-run by default).

       When to use: exporting many sizes/formats/objects in one call. For a single export use
       `export_document` / `export_object`; for a standard icon set use `create_icon_set`.

       Key params: `specs` is a typed list (each: `format` png/pdf/svg, optional `width_px`, optional
       `object_id` for a single object). Bounded: at most a fixed number of specs per call and a
       total-output byte budget (`byte_budget`, default: the per-document artifact budget).
       `dry_run=True` (DEFAULT) validates and returns the plan + projected sizes + `within_budget`,
       writing nothing; `dry_run=False` refuses cleanly if the projection exceeds the budget. `out_dir`
    writes into a caller-chosen dir — relative anchors to the workspace ROOT,
       sandbox-checked (out-of-workspace rejected "path rejected: outside workspace"); `name_prefix`
       tags each file.

       Return shape: `BatchResult` — `item_count`, per-item entries (each with a
       `workspace_relative_path` on a real run), projected/actual total size, and `within_budget`.

       Example: `export_batch(doc_id, [{"format": "png", "width_px": 256}], dry_run=False)`

       Risk class: low (artifact-only export to a sandbox-checked dir; composes the engine).
    """
    try:
        result = _export_batch(
            doc_id,
            specs,
            dry_run=dry_run,
            byte_budget=byte_budget,
            out_dir=out_dir,
            name_prefix=name_prefix,
        )
    except (
        KeyError,
        DocumentNotFound,
        InspectionError,
        SandboxViolation,
        BatchError,
        LimitExceeded,
        RenderError,
        ProcessError,
    ) as exc:
        _logger.error("export_batch failed", extra={"doc_id": doc_id, "detail": str(exc)})
        raise _map_failure(exc) from exc

    log_tool_call(
        _logger,
        tool="export_batch",
        doc_id=doc_id,
        dry_run=dry_run,
        items=result.item_count,
    )
    return result


class ExportSetEntry(BaseModel):
    """One document's batch-export result inside an :class:`ExportSetResult`."""

    doc_id: str
    result: BatchResult


class ExportSetResult(BaseModel):
    """Result of `export_set`: per-doc batch results + aggregate + consistency verdict.

    `per_doc` is one :class:`ExportSetEntry` per input document (each carrying the SAME
    `BatchResult` the single-doc `export_batch` returns). `total_bytes` is the sum of every per-doc
    projected (dry run) or actual (real run) output size — the set's whole byte footprint in one
    number. `total_items` is the count of planned/exported artifacts across the set. `consistency`
    is the structured cross-doc verdict (viewBox / stroke-width / id-naming agreement).
    """

    per_doc: list[ExportSetEntry]
    total_items: int
    total_bytes: int
    dry_run: bool
    consistency: ConsistencyVerdict


@mcp.tool
def export_set(
    doc_ids: list[str],
    specs: list[ExportSpec],
    dry_run: bool = True,
    byte_budget: int | None = None,
    out_dir: str | None = None,
    name_prefix: str | None = None,
) -> ExportSetResult:
    """Batch-export a SET of documents in one call: per-doc results + aggregate + verdict.

    When to use: exporting a whole multi-document system (e.g. a 12-icon set) at the same sizes /
    formats in one call, with the set's total byte footprint and a cross-doc consistency check. For
    a SINGLE document use `export_batch`; for an icon set from one doc use `create_icon_set`.

    Key params: `doc_ids` is a non-empty, duplicate-free set; `specs` is the SAME typed `ExportSpec`
    list `export_batch` takes (applied to EVERY document). `dry_run` / `byte_budget` / `out_dir` /
    `name_prefix` behave exactly as on `export_batch` (composed, not reimplemented), per document; a
    `name_prefix` is recommended with `out_dir` so the per-doc files do not collide. The whole set
    is rejected if ANY document's `export_batch` fails (no partial result).

    Return shape: `ExportSetResult` — `per_doc` (each `{doc_id, result}` with the standard
    `BatchResult`), `total_items` and `total_bytes` aggregated across the set (projected on a dry
    run, actual on a real run), and `consistency` — the structured cross-doc verdict over the set's
    viewBox / stroke-width / id-naming conventions (per property: agree/disagree + the differing
    values + which doc_ids differ).

    Example: `export_set(["d1","d2","d3"], [{"format": "png", "width_px": 64}], dry_run=False,
    out_dir="dist", name_prefix="icon")`

    Risk class: low (artifact-only export to a sandbox-checked dir; composes the per-doc engine).
    """
    require_unique_doc_ids(doc_ids)
    # Build the cross-doc verdict FIRST (read-only; surfaces an unknown/unparseable id before any
    # export runs).
    consistency = build_set_verdict(doc_ids)

    entries: list[ExportSetEntry] = []
    total_items = 0
    total_bytes = 0
    for doc_id in doc_ids:
        try:
            result = _export_batch(
                doc_id,
                specs,
                dry_run=dry_run,
                byte_budget=byte_budget,
                out_dir=out_dir,
                name_prefix=name_prefix,
            )
        except (
            KeyError,
            DocumentNotFound,
            InspectionError,
            SandboxViolation,
            BatchError,
            LimitExceeded,
            RenderError,
            ProcessError,
        ) as exc:
            _logger.error("export_set failed", extra={"doc_id": doc_id, "detail": str(exc)})
            raise _map_failure(exc) from exc
        entries.append(ExportSetEntry(doc_id=doc_id, result=result))
        total_items += result.item_count
        # Aggregate the same number the per-doc result reports: actual output on a real run, the
        # conservative projection on a dry run (stat machinery semantics).
        total_bytes += (
            result.actual_total_bytes
            if result.actual_total_bytes is not None
            else result.projected_total_bytes
        )

    log_tool_call(
        _logger,
        tool="export_set",
        docs=len(doc_ids),
        dry_run=dry_run,
        total_items=total_items,
        consistent=consistency.consistent,
    )
    return ExportSetResult(
        per_doc=entries,
        total_items=total_items,
        total_bytes=total_bytes,
        dry_run=dry_run,
        consistency=consistency,
    )
