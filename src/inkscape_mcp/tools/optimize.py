"""Web-optimize tool: `svg_web_optimize`.

Thin MCP layer over the direct-DOM web-optimization engine (`inkscape_mcp.edit.optimize`). Small
and typed (ADR-002, no portmanteau): it validates the trivial argument shape, builds the engine's
`mutate` closure, and hands it to the shared edit pipeline (`apply_edit`) so the change is
reversible and audited — a pre-mutation snapshot plus an Operation Record with a linked
before/after preview (ADR-004). The cleanup is direct DOM via lxml (ADR-005); no Inkscape engine is
invoked for the metadata/id/group pruning. Policy classifies it as **medium** risk (mutates the
working copy).

Client-facing errors are raised as `ToolError` with stable, host-path-free messages (sec.12):
unknown document id -> "document id not found"; an unparseable working copy -> "document could not
be parsed safely"; an invalid `precision` -> the validation message (built from a typed parameter,
no host path).
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from inkscape_mcp.document.inspect import DocumentNotFound, InspectionError
from inkscape_mcp.edit.collection import ConsistencyVerdict
from inkscape_mcp.edit.dom import EditError, TargetNotFound
from inkscape_mcp.edit.optimize import (
    DEFAULT_PRECISION,
    WebOptimizeDeltas,
    optimize_web_mutate,
)
from inkscape_mcp.edit.pipeline import EditApplyError, EditResult, apply_edit
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.server import mcp
from inkscape_mcp.tools.collection_set import build_set_verdict, require_unique_doc_ids

_logger = get_logger("tools.optimize")


class WebOptimizeResult(EditResult):
    """`svg_web_optimize` result: the standard reversible-edit fields plus machine-diffable deltas.

    Extends :class:`EditResult` (so `operation_id` / `snapshot_id` / previews / `summary` are
    unchanged for existing callers) with the structured deltas: `bytes_before` /
    `bytes_after` (serialized working-copy sizes around the optimize, so `bytes_before -
    bytes_after` is the saving with no on-disk ``stat``) and `removed` — a ``{code: count}`` map
    keyed IDENTICALLY to ``quality_report.opportunities`` (``editor_metadata``, ``unused_defs``,
    ``unreferenced_ids``, ``empty_groups``, ``reducible_coords``), listing only the cleanups that
    actually removed/rewrote something. An empty `removed` map means the pass was a no-op.
    """

    bytes_before: int
    bytes_after: int
    removed: dict[str, int]


#: Exceptions raised by the engine / pipeline that this layer maps to a stable `ToolError`.
_MAPPED = (
    EditApplyError,
    DocumentNotFound,
    KeyError,
    TargetNotFound,
    InspectionError,
    EditError,
)


def _map_failure(exc: Exception) -> ToolError:
    """Map an engine/pipeline exception to a stable, host-path-free `ToolError` (sec.12)."""
    if isinstance(exc, (EditApplyError, DocumentNotFound, KeyError)):
        return ToolError("document id not found")
    if isinstance(exc, InspectionError):
        return ToolError("document could not be parsed safely")
    if isinstance(exc, (EditError, TargetNotFound)):
        # Validation message, built from typed parameters — already safe (no host path).
        return ToolError(str(exc))
    return ToolError("optimize failed")


@mcp.tool
def svg_web_optimize(
    doc_id: str,
    precision: int = DEFAULT_PRECISION,
    keep_ids: list[str] | None = None,
) -> WebOptimizeResult:
    """Web-optimize an SVG: strip editor metadata, drop dead structure, reduce coordinate precision.

    When to use: losslessly shrinking an SVG for the web (direct-DOM, non-destructive). To inspect
    what WOULD be stripped first use `quality_report`; for lossy node reduction use `simplify_path`.

    Key params: three reversible cleanups — (1) remove Inkscape/sodipodi editor-only elements,
    namespaced attributes, and XML comments; (2) drop unreferenced `<defs>`, every unreferenced
    `id`, and empty groups (referenced ids preserved so no `#frag` / `url(#frag)` / `href` breaks);
    (3) round geometry numbers (path `d`, transforms, `x`/`y`/`width`/…) to `precision` decimals
    (0-8, default 2; root `viewBox` untouched). `keep_ids` is an allowlist of ids that must NEVER be
    stripped as "unreferenced" — pass a deliberate human/a11y id (e.g. one from `rename_object`) to
    keep "one clean file with a stable id"; unknown ids are ignored. Re-running on
    optimized output removes/rounds nothing further.

    Return shape: `WebOptimizeResult` — the reversible-edit fields (`operation_id`, `snapshot_id`,
    before/after preview) plus machine-diffable deltas `bytes_before`, `bytes_after`, and `removed`
    (a ``{code: count}`` map keyed IDENTICALLY to `quality_report.opportunities`), so an agent on a
    byte budget can compute the saving without parsing prose or stat-ing the file.

    Example: `svg_web_optimize(doc_id, precision=2, keep_ids=["header"])`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium.
    """
    return _optimize_one(doc_id, precision, keep_ids)


def _optimize_one(doc_id: str, precision: int, keep_ids: list[str] | None) -> WebOptimizeResult:
    """Run ONE web-optimize through the reversible pipeline and fold in the machine-diffable deltas.

    The shared core of `svg_web_optimize` and `optimize_set` (each doc) — so the set tool COMPOSES
    the single-doc engine rather than reimplementing it. Maps engine/pipeline exceptions to a
    stable, host-path-free `ToolError` (sec.12).
    """
    deltas_holder: list[WebOptimizeDeltas] = []
    try:
        mutate = optimize_web_mutate(precision, keep_ids=keep_ids, deltas_holder=deltas_holder)
        result = apply_edit(
            doc_id,
            "svg_web_optimize",
            {"precision": precision, "keep_ids": keep_ids},
            mutate,
        )
    except _MAPPED as exc:
        raise _map_failure(exc) from exc

    # `deltas_holder` is always populated by the mutate on a successful apply (the only path that
    # reaches here); default to a zero-delta no-op map if a future change leaves it empty.
    deltas = (
        deltas_holder[0]
        if deltas_holder
        else WebOptimizeDeltas(bytes_before=0, bytes_after=0, removed={})
    )

    log_tool_call(
        _logger,
        tool="svg_web_optimize",
        doc_id=doc_id,
        operation_id=result.operation_id,
        bytes_saved=deltas.bytes_saved,
    )
    return WebOptimizeResult(
        **result.model_dump(),
        bytes_before=deltas.bytes_before,
        bytes_after=deltas.bytes_after,
        removed=deltas.removed,
    )


class OptimizeSetEntry(BaseModel):
    """One document's web-optimize result inside an :class:`OptimizeSetResult`."""

    doc_id: str
    result: WebOptimizeResult


class OptimizeSetResult(BaseModel):
    """Result of `optimize_set`: per-doc optimize results + aggregate + verdict.

    `per_doc` is one :class:`OptimizeSetEntry` per input document (each carrying the SAME
    `WebOptimizeResult` the single-doc `svg_web_optimize` returns, incl. its `operation_id` /
    `snapshot_id` — one snapshot per CHANGED document). `total_bytes_before` / `total_bytes_after` /
    `total_bytes_saved` aggregate the saving across the whole set. `changed_count` is how many
    documents actually changed (a no-op writes no snapshot). `consistency` is the structured
    cross-doc verdict computed on the PRE-optimize state (so a viewBox / stroke-width / id-naming
    mismatch in the inputs is surfaced).
    """

    per_doc: list[OptimizeSetEntry]
    total_bytes_before: int
    total_bytes_after: int
    total_bytes_saved: int
    changed_count: int
    consistency: ConsistencyVerdict


@mcp.tool
def optimize_set(
    doc_ids: list[str],
    precision: int = DEFAULT_PRECISION,
    keep_ids: list[str] | None = None,
) -> OptimizeSetResult:
    """Web-optimize a SET of documents in one call: per-doc results + aggregate + verdict.

    When to use: losslessly shrinking a whole multi-document system (e.g. a 12-icon set) in one
    call, reading the set's total byte saving and a cross-doc consistency check. For a SINGLE
    document use `svg_web_optimize`; to inspect the opportunities use `quality_report_set`.

    Key params: `doc_ids` is a non-empty, duplicate-free set; `precision` / `keep_ids` are the SAME
    arguments `svg_web_optimize` takes (applied to EVERY document). Each document is optimized
    through the reversible pipeline, so a CHANGED document gets ONE pre-mutation snapshot +
    Operation Record (ADR-004) and a no-op writes none. The whole set is rejected if ANY document
    fails (no partial apply on the remainder is suppressed — earlier successful docs stay optimized
    and reversible via their snapshots).

    Return shape: `OptimizeSetResult` — `per_doc` (each `{doc_id, result}` with the standard
    `WebOptimizeResult`), `total_bytes_before` / `total_bytes_after` / `total_bytes_saved`
    aggregated across the set, `changed_count`, and `consistency` — the structured cross-doc verdict
    computed on the PRE-optimize state (per property: agree/disagree + the differing values + which
    doc_ids differ).

    Example: `optimize_set(["d1","d2","d3"], precision=2)`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (each document optimized reversibly; one snapshot per changed doc).
    """
    require_unique_doc_ids(doc_ids)
    # Verdict on the PRE-optimize state (read-only; surfaces an unknown/unparseable id before any
    # mutation), so an input viewBox / stroke / id-naming mismatch is reported as-found.
    consistency = build_set_verdict(doc_ids)

    entries: list[OptimizeSetEntry] = []
    total_before = 0
    total_after = 0
    changed = 0
    for doc_id in doc_ids:
        result = _optimize_one(doc_id, precision, keep_ids)
        entries.append(OptimizeSetEntry(doc_id=doc_id, result=result))
        total_before += result.bytes_before
        total_after += result.bytes_after
        if result.changed:
            changed += 1

    log_tool_call(
        _logger,
        tool="optimize_set",
        docs=len(doc_ids),
        changed=changed,
        bytes_saved=total_before - total_after,
        consistent=consistency.consistent,
    )
    return OptimizeSetResult(
        per_doc=entries,
        total_bytes_before=total_before,
        total_bytes_after=total_after,
        total_bytes_saved=total_before - total_after,
        changed_count=changed,
        consistency=consistency,
    )
