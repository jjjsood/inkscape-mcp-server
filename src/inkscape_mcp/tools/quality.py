"""Quality-report tool (E5-05): `quality_report`.

Thin MCP layer over the read-only quality engine (`inkscape_mcp.quality`), which extends the E1-08
validation engine with quantitative metrics and optimization opportunities. Direct DOM only
(ADR-005); no mutation, no Operation Record, no network. Risk class **low** (read-only). All
client-facing errors are raised as `ToolError` with stable, host-path-free messages (sec.12).
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from inkscape_mcp.edit.collection import ConsistencyVerdict
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.quality import (
    DocumentNotFound,
    InspectionError,
    QualityReport,
)
from inkscape_mcp.quality import (
    quality_report as _quality_report,
)
from inkscape_mcp.server import mcp
from inkscape_mcp.tools.collection_set import build_set_verdict, require_unique_doc_ids

_logger = get_logger("tools.quality")


@mcp.tool
def quality_report(doc_id: str) -> QualityReport:
    """Build a machine-readable quality report for a document: validation findings plus metrics.

    When to use: assessing a document's health and what optimizing would save. For pass/fail
    correctness only use `validate_document`; to actually strip the opportunities use
    `svg_web_optimize`.

    Key params: none beyond `doc_id`.

    Return shape: `QualityReport` — `ok`, the `validate_document` findings (missing fonts, external
    assets, large rasters, id problems, viewBox sanity), quantitative metrics (object/node/layer
    counts, embedded-raster weight in bytes, font coverage, viewBox health), and `opportunities`
    (keyed identically to `svg_web_optimize.removed`: editor metadata, unused defs, unreferenced
    ids, empty groups, reducible coordinate precision). Every field is structured (not prose).

    Example: `quality_report(doc_id)`

    Risk class: low (read-only; document unchanged).
    """
    try:
        report = _quality_report(doc_id)
    except (DocumentNotFound, KeyError) as exc:
        raise ToolError("document id not found") from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc

    log_tool_call(
        _logger,
        tool="quality_report",
        doc_id=doc_id,
        ok=report.ok,
        opportunities=len(report.opportunities),
    )
    return report


class QualityReportSetResult(BaseModel):
    """Result of `quality_report_set` (E16-05): per-doc reports + aggregate + consistency verdict.

    `per_doc` is one `QualityReport` per input document (the SAME report the single-doc
    `quality_report` returns). `all_ok` is True iff every document is valid; `worst_score` /
    `mean_score` summarise the set's health for triage; `total_opportunities` is the count of
    optimization opportunities across the set. `consistency` is the structured cross-doc verdict
    (viewBox / stroke-width / id-naming agreement — per property: agree/disagree + differing values
    + which doc_ids differ).
    """

    per_doc: list[QualityReport]
    all_ok: bool
    worst_score: int
    mean_score: int
    total_opportunities: int
    consistency: ConsistencyVerdict


@mcp.tool
def quality_report_set(doc_ids: list[str]) -> QualityReportSetResult:
    """Quality-report a SET of documents in one call: per-doc reports + aggregate + verdict.

    When to use: auditing a whole multi-document system (e.g. a 12-icon set) for health AND
    cross-doc consistency in one read-only call. For a SINGLE document use `quality_report`; to
    actually strip the opportunities across the set use `optimize_set`.

    Key params: `doc_ids` is a non-empty, duplicate-free set. Read-only — composes the single-doc
    `quality_report` engine over the set, so NO snapshot / Operation Record is written for any
    document. The whole set is rejected if ANY id is unknown or unparseable (no partial result).

    Return shape: `QualityReportSetResult` — `per_doc` (the standard `QualityReport` per document),
    `all_ok`, `worst_score` / `mean_score` and `total_opportunities` aggregated across the set, and
    `consistency` — the structured cross-doc verdict over the set's viewBox / stroke-width /
    id-naming conventions (the cross-doc audit a 12-icon system used to need a Bash/lxml loop for).

    Example: `quality_report_set(["d1","d2","d3"])`

    Risk class: low (read-only; no document mutated, no Operation Record / snapshot).
    """
    require_unique_doc_ids(doc_ids)
    consistency = build_set_verdict(doc_ids)

    reports: list[QualityReport] = []
    for doc_id in doc_ids:
        try:
            reports.append(_quality_report(doc_id))
        except (DocumentNotFound, KeyError) as exc:
            raise ToolError("document id not found") from exc
        except InspectionError as exc:
            raise ToolError("document could not be parsed safely") from exc

    scores = [r.score for r in reports]
    total_opportunities = sum(len(r.opportunities) for r in reports)

    log_tool_call(
        _logger,
        tool="quality_report_set",
        docs=len(doc_ids),
        all_ok=all(r.ok for r in reports),
        consistent=consistency.consistent,
    )
    return QualityReportSetResult(
        per_doc=reports,
        all_ok=all(r.ok for r in reports),
        worst_score=min(scores),
        mean_score=round(sum(scores) / len(scores)),
        total_opportunities=total_opportunities,
        consistency=consistency,
    )
