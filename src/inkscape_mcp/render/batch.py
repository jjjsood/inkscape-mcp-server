"""Batch-export engine (E5-06).

Pure functions (no MCP decorators) that compose the E1-06 CLI render/export engine
(`inkscape_mcp.render.cli`) into a single bounded, dry-run-by-default batch run. A batch is a list
of TYPED export specs (NOT a string / portmanteau — ADR-002); the engine adds only the bounds that
keep a batch primitive from becoming an unbounded export hole:

- a **per-call item cap** (`MAX_BATCH_ITEMS`) — refuses an oversized or empty batch up front;
- a **total-output byte budget** — a conservative projection of every item's size is summed and
  compared to the budget; a real run that would exceed it is refused cleanly BEFORE any export runs;
- **dry-run by default** — `dry_run=True` validates every spec and reports the planned exports plus
  projected sizes, writing NOTHING.

All exports delegate to `export_document` / `export_object`, which already enforce the §4 export
limits (pixel cap before invocation, output-size cap after), the per-process timeout, and
arg-lists-only invocation (`shell=False`) per sec.12 — this module adds no new authority. Object ids
are charset-validated and existence-checked here (fail-fast) before any spec runs. Returned artifact
paths follow ONE LOCATION CONTRACT (E11-01): `artifact_path` and `workspace_relative_path` carry the
SAME value, always relative to the WORKSPACE ROOT; originals / working copies are never overwritten.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.document.inspect import (
    DocSummary,
    DocumentNotFound,
    InspectionError,
    inspect_objects,
    inspect_summary,
)
from inkscape_mcp.logging_setup import get_logger
from inkscape_mcp.registry import Registry, get_registry
from inkscape_mcp.render.cli import (
    _resolve_out_dir,
    _target_raster_dims,
    export_document,
    export_object,
    is_safe_object_id,
)

_logger = get_logger("render.batch")

#: Per-call item cap: a batch may carry at most this many specs (ADR/sec.12 bound).
MAX_BATCH_ITEMS = 32

#: Export formats a spec may request (validated before any invocation).
_FORMATS = frozenset({"png", "pdf", "svg"})

#: Floor for the vector (PDF/SVG) size projection — these outputs have no raster dimension to size
#: against, so they are projected from the working-copy byte size (never below this).
_VECTOR_PROJECTION_FLOOR = 4096


class BatchError(Exception):
    """A batch was malformed, exceeded the item cap, or exceeded the byte budget.

    Carries a stable, host-path-free public message; the tool layer maps it to `ToolError`.
    """


class ExportSpec(BaseModel):
    """One typed export request within a batch.

    `format` is one of png / pdf / svg. `width_px` (PNG only) scales the raster; PDF/SVG ignore it.
    `object_id`, when set, exports just that object (clipped to its bbox); otherwise the whole
    document is exported.
    """

    format: str
    width_px: int | None = None
    object_id: str | None = None


class BatchItem(BaseModel):
    """The plan/outcome for one spec.

    ONE LOCATION CONTRACT (E11-01): `artifact_path` and `workspace_relative_path` carry the SAME
    value — the file relative to the WORKSPACE ROOT, openable by a single join to the root with no
    `find`/`stat` — and are set only on a real (non-dry) run. `artifact_path` is kept only for
    back-compat and now means exactly the same thing.
    """

    index: int
    format: str
    width_px: int | None
    object_id: str | None
    projected_bytes: int
    artifact_path: str | None = None
    workspace_relative_path: str | None = None
    status: str  # "planned" (dry run) | "exported" (real run)


class BatchResult(BaseModel):
    """Outcome of a batch run.

    `projected_total_bytes` is the conservative sum used for the budget check; `actual_total_bytes`
    is the real summed output size (None on a dry run). `within_budget` reflects the projection vs.
    `byte_budget`.
    """

    doc_id: str
    dry_run: bool
    item_count: int
    byte_budget: int
    projected_total_bytes: int
    actual_total_bytes: int | None
    within_budget: bool
    items: list[BatchItem] = Field(default_factory=list)


def _settings(settings: Settings | None) -> Settings:
    return settings if settings is not None else get_settings()


def _project_bytes(summary: DocSummary, fmt: str, width_px: int | None, working_size: int) -> int:
    """A conservative upper-bound projection of one export's output size (no Inkscape invoked).

    PNG: the target raster `w*h*4` (RGBA upper bound; a compressed PNG of those dims is no larger).
    PDF/SVG: proxied by the working-copy byte size (floored), since a vector output has no raster
    size to compute and tracks document complexity.
    """
    if fmt == "png":
        target_w, target_h = _target_raster_dims(summary, width_px)
        return target_w * target_h * 4
    return max(working_size, _VECTOR_PROJECTION_FLOOR)


def _validate_spec(
    spec: ExportSpec, index: int, settings: Settings, object_ids: set[str] | None
) -> str:
    """Validate one spec (format / width / object id) and return the normalized format token.

    `object_ids` is the set of ids present in the document, computed once by the caller only when at
    least one spec targets an object. Raises `BatchError` on the first problem (fail-fast, before
    any export runs).
    """
    fmt = spec.format.lower().strip()
    if fmt not in _FORMATS:
        raise BatchError(
            f"spec {index}: unsupported format (expected one of: png, pdf, svg); "
            f"call list_capabilities to see the supported export formats"
        )
    if spec.width_px is not None:
        if spec.width_px <= 0:
            raise BatchError(f"spec {index}: width_px must be a positive integer")
        if spec.width_px > settings.max_export_px:
            raise BatchError(f"spec {index}: width_px exceeds the configured pixel cap")
    if spec.object_id is not None:
        if not is_safe_object_id(spec.object_id):
            raise BatchError(f"spec {index}: object id is not valid")
        if object_ids is not None and spec.object_id not in object_ids:
            raise BatchError(f"spec {index}: object id not found in document")
    return fmt


def export_batch(
    doc_id: str,
    specs: list[ExportSpec],
    dry_run: bool = True,
    byte_budget: int | None = None,
    out_dir: str | None = None,
    name_prefix: str | None = None,
    settings: Settings | None = None,
    registry: Registry | None = None,
) -> BatchResult:
    """Plan (and, unless `dry_run`, run) a bounded batch export.

    Refuses an empty batch or one over `MAX_BATCH_ITEMS`, validates every spec, projects each
    output's size, and sums it. `byte_budget` defaults to the per-document artifact byte budget and
    is clamped DOWN to it (a caller may only tighten the bound, never widen it). On a dry run,
    returns the plan (no writes). On a real run, refuses cleanly if the projected total exceeds the
    budget, otherwise runs each export through the E1-06 engine — aborting if the actual cumulative
    output crosses the budget mid-run — and returns resolvable artifact paths plus the actual total
    size. An optional `out_dir` (relative paths anchored to the workspace ROOT, then
    sandbox-validated) and `name_prefix` are forwarded to every export (E11-05).

    Raises `BatchError` for a bad/oversized/over-budget batch, `DocumentNotFound` for an unknown id,
    `SandboxViolation` for an out-of-workspace `out_dir`, and re-raises the engine's `RenderError` /
    `LimitExceeded` / `ProcessError` from a real export.
    """
    s = _settings(settings)
    reg = registry if registry is not None else get_registry()

    if not specs:
        raise BatchError("export batch requires at least one spec")
    if len(specs) > MAX_BATCH_ITEMS:
        raise BatchError(f"export batch exceeds the item cap ({MAX_BATCH_ITEMS})")

    budget = byte_budget if byte_budget is not None else s.artifact_max_bytes_per_doc
    if budget <= 0:
        raise BatchError("byte budget must be a positive integer")
    # A client-supplied budget may only LOWER the configured per-document artifact cap, never raise
    # it — otherwise a huge `byte_budget` would defeat the only batch-wide size gate (sec.12).
    budget = min(budget, s.artifact_max_bytes_per_doc)

    try:
        entry = reg.get(doc_id)
    except KeyError:
        raise DocumentNotFound("document id not found") from None

    # Validate a caller-chosen out_dir UP FRONT (even on a dry run) so an out-of-workspace target
    # is rejected with "path rejected: outside workspace" before any planning/writes (E11-05).
    if out_dir is not None:
        _resolve_out_dir(out_dir, entry, s)

    # Resolve the document once (raises DocumentNotFound / InspectionError, mapped by the tool).
    summary = inspect_summary(doc_id)
    working_size = Path(entry.working_path).stat().st_size

    # Only inspect objects if some spec targets one (avoids an extra parse for whole-doc batches).
    object_ids: set[str] | None = None
    if any(spec.object_id is not None for spec in specs):
        object_ids = {obj.id for obj in inspect_objects(doc_id).objects if obj.id is not None}

    # Validate + project every spec up front (fail-fast, no writes yet).
    planned: list[BatchItem] = []
    projected_total = 0
    for index, spec in enumerate(specs):
        fmt = _validate_spec(spec, index, s, object_ids)
        projected = _project_bytes(summary, fmt, spec.width_px, working_size)
        projected_total += projected
        planned.append(
            BatchItem(
                index=index,
                format=fmt,
                width_px=spec.width_px,
                object_id=spec.object_id,
                projected_bytes=projected,
                status="planned",
            )
        )

    within_budget = projected_total <= budget

    if dry_run:
        _logger.info(
            "export batch planned (dry run)",
            extra={
                "doc_id": doc_id,
                "items": len(planned),
                "projected_total_bytes": projected_total,
                "within_budget": within_budget,
            },
        )
        return BatchResult(
            doc_id=doc_id,
            dry_run=True,
            item_count=len(planned),
            byte_budget=budget,
            projected_total_bytes=projected_total,
            actual_total_bytes=None,
            within_budget=within_budget,
            items=planned,
        )

    if not within_budget:
        raise BatchError(
            f"export batch projected output exceeds the byte budget "
            f"({projected_total} > {budget} bytes)"
        )

    # Real run: every spec is already validated; export each through the E1-06 engine. The first
    # export validates `out_dir` (raising SandboxViolation before any write if it escapes the
    # sandbox); the same params are forwarded to every spec.
    items: list[BatchItem] = []
    actual_total = 0
    for item in planned:
        if item.object_id is None:
            result = export_document(
                doc_id,
                item.format,
                width_px=item.width_px,
                out_dir=out_dir,
                name_prefix=name_prefix,
                settings=s,
            )
        else:
            result = export_object(
                doc_id,
                item.object_id,
                item.format,
                width_px=item.width_px,
                out_dir=out_dir,
                name_prefix=name_prefix,
                settings=s,
            )
        # Stat via the root-anchored resolvable path (works whether the artifact landed in the
        # managed per-doc dir or a caller-chosen out_dir).
        size = (Path(entry.root) / result.workspace_relative_path).stat().st_size
        actual_total += size
        # Backstop against a projection that under-estimated (e.g. a complex SVG export): abort the
        # moment the real cumulative output crosses the budget, so a batch can never run unbounded.
        if actual_total > budget:
            raise BatchError(
                f"export batch output exceeded the byte budget mid-run "
                f"({actual_total} > {budget} bytes)"
            )
        items.append(
            BatchItem(
                index=item.index,
                format=item.format,
                width_px=item.width_px,
                object_id=item.object_id,
                projected_bytes=item.projected_bytes,
                artifact_path=result.artifact_path,
                workspace_relative_path=result.workspace_relative_path,
                status="exported",
            )
        )

    _logger.info(
        "export batch complete",
        extra={"doc_id": doc_id, "items": len(items), "actual_total_bytes": actual_total},
    )
    return BatchResult(
        doc_id=doc_id,
        dry_run=False,
        item_count=len(items),
        byte_budget=budget,
        projected_total_bytes=projected_total,
        actual_total_bytes=actual_total,
        within_budget=within_budget,
        items=items,
    )


__all__ = [
    "MAX_BATCH_ITEMS",
    "BatchError",
    "BatchItem",
    "BatchResult",
    "DocumentNotFound",
    "ExportSpec",
    "InspectionError",
    "export_batch",
]
