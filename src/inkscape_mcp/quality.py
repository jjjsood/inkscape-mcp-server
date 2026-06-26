"""Machine-readable quality report engine (read-only, ADR-005 direct DOM).

Pure functions over the WORKING COPY of a registered document. Extends the validation engine
(`inkscape_mcp.validate`) rather than duplicating it: a `quality_report` wraps the
`validate_document` findings and adds quantitative METRICS (object/node counts, embedded-raster
weight, font coverage, viewBox health) plus OPTIMIZATION OPPORTUNITIES sourced from the
optimizer (`inkscape_mcp.edit.optimize.analyze_optimizations`) so the opportunities it names are
exactly what `svg_web_optimize` would strip.

READ-ONLY by contract (sec.12): nothing here mutates the working copy or the original. No MCP
decorators; the tool layer (`inkscape_mcp.tools.quality`) wraps `quality_report` and maps errors to
`ToolError`. The returned `QualityReport` is a structured pydantic model — machine-readable, not
prose.
"""

from __future__ import annotations

from lxml import etree
from pydantic import BaseModel

from inkscape_mcp.document.inspect import (
    DocumentNotFound,
    InspectionError,
    _load_tree,
    inspect_fonts,
    inspect_summary,
)
from inkscape_mcp.edit.optimize import DEFAULT_PRECISION, analyze_optimizations
from inkscape_mcp.logging_setup import get_logger
from inkscape_mcp.registry import Registry, get_registry
from inkscape_mcp.validate import (
    Finding,
    _data_uri_decoded_size,
    _href_of,
    _installed_font_families,
    validate_document,
)

__all__ = [
    "DocumentNotFound",
    "FontCoverage",
    "InspectionError",
    "OptimizationOpportunity",
    "QualityMetrics",
    "QualityReport",
    "ViewBoxHealth",
    "quality_report",
]

_logger = get_logger("quality")

#: Generic CSS font keywords that are always resolvable — never counted as missing.
_GENERIC_FONTS = frozenset(
    {
        "serif",
        "sans-serif",
        "monospace",
        "cursive",
        "fantasy",
        "system-ui",
        "ui-serif",
        "ui-sans-serif",
        "ui-monospace",
        "ui-rounded",
        "math",
        "emoji",
        "fangsong",
    }
)

#: Human-readable hints for each optimization opportunity code (all removed by `svg_web_optimize`).
_OPPORTUNITY_MESSAGES = {
    "editor_metadata": "editor-only metadata/attributes/comments can be stripped",
    "unused_defs": "unreferenced <defs> templates can be removed",
    "unreferenced_ids": "id attributes nothing references can be dropped",
    "empty_groups": "empty groups can be removed",
    "reducible_coords": "coordinate precision can be reduced",
}


class FontCoverage(BaseModel):
    """Font availability for the document.

    `referenced` is the count of distinct referenced family names (generic keywords excluded);
    `installed` is how many of those resolve to an installed font; `missing` lists the unresolved
    families. `checked` is False when the font database (`fc-list`) is unavailable — then
    `installed` is 0 and `missing` is empty (the check was skipped, not "all missing").
    """

    referenced: int
    installed: int
    missing: list[str]
    checked: bool


class ViewBoxHealth(BaseModel):
    """viewBox presence / sanity. `valid` is True iff present with positive width and height."""

    present: bool
    valid: bool
    width: float | None
    height: float | None


class QualityMetrics(BaseModel):
    """Quantitative document metrics (all read-only)."""

    object_count: int
    node_count: int
    layer_count: int
    embedded_raster_count: int
    embedded_raster_bytes: int
    font_coverage: FontCoverage
    viewbox: ViewBoxHealth


class OptimizationOpportunity(BaseModel):
    """One optimization opportunity — consistent with what `svg_web_optimize` would strip.

    `code` is a stable machine identifier; `count` is how many items would be cleaned up; `message`
    is human-readable and host-path-free.
    """

    code: str
    count: int
    message: str


class QualityReport(BaseModel):
    """Structured, machine-readable quality report for one document.

    Wraps the validation result (`findings` + `ok` + tallies) and adds `metrics`,
    optimization `opportunities`, and a rolled-up `score`. Not prose — every field is typed.

    `score` is a 0-100 triage HEURISTIC: 100 is a clean document; each validation error,
    each warning, and each optimization opportunity subtracts a bounded penalty. It is deliberately
    a coarse summary for ranking/triage - the per-code `opportunities` and typed `findings` stay the
    authoritative, actionable detail (a low score never replaces reading them).
    """

    doc_id: str
    ok: bool
    score: int
    findings: list[Finding]
    error_count: int
    warning_count: int
    metrics: QualityMetrics
    opportunities: list[OptimizationOpportunity]


def _font_coverage(doc_id: str) -> FontCoverage:
    """Compute font coverage: referenced families vs. installed, generic keywords excluded."""
    referenced = [
        f.family.strip()
        for f in inspect_fonts(doc_id).fonts
        if f.family.strip() and f.family.strip().lower() not in _GENERIC_FONTS
    ]
    installed, _note = _installed_font_families()
    if installed is None:
        return FontCoverage(referenced=len(referenced), installed=0, missing=[], checked=False)
    missing_all = [fam for fam in referenced if fam.lower() not in installed]
    # Bound the surfaced list (count stays exact): a document-sourced family name could be very long
    # or crafted, so truncate each name and cap the list before returning it to the client.
    missing = [fam[:120] for fam in missing_all[:64]]
    return FontCoverage(
        referenced=len(referenced),
        installed=len(referenced) - len(missing_all),
        missing=missing,
        checked=True,
    )


def _raster_weight(root: etree._Element) -> tuple[int, int]:
    """(count, total decoded bytes) of embedded `data:` raster `<image>` payloads. No network."""
    count = 0
    total = 0
    for elem in root.iter():
        if not isinstance(elem.tag, str):
            continue
        if etree.QName(elem.tag).localname != "image":
            continue
        href = _href_of(elem)
        if not href:
            continue
        size = _data_uri_decoded_size(href)
        if size is None:  # external image — not an embedded raster
            continue
        count += 1
        total += size
    return count, total


def _node_count(root: etree._Element) -> int:
    """Total element nodes in the document (comments / PIs excluded)."""
    return sum(1 for node in root.iter() if isinstance(node.tag, str))


#: Per-item penalties for the rolled-up quality `score`. Errors weigh most (they make the
#: document invalid), warnings less, and each optimization opportunity a small bounded amount.
_SCORE_ERROR_PENALTY = 15
_SCORE_WARNING_PENALTY = 5
#: A single opportunity code contributes at most this much, so one very high `count` cannot alone
#: sink the score — the codes are advisory clean-ups, not correctness defects.
_SCORE_OPPORTUNITY_CAP = 5


def _quality_score(
    error_count: int, warning_count: int, opportunities: list[OptimizationOpportunity]
) -> int:
    """Roll the findings + opportunities into a 0-100 triage heuristic (100 = clean).

    Coarse by design: subtract a fixed penalty per validation error and per warning, plus a small
    bounded penalty per optimization opportunity (each capped so a single large `count` cannot
    dominate). Clamped to ``[0, 100]``. This is a ranking aid only - the typed `findings` and
    per-code `opportunities` remain the authoritative detail.
    """
    penalty = error_count * _SCORE_ERROR_PENALTY + warning_count * _SCORE_WARNING_PENALTY
    penalty += sum(min(o.count, _SCORE_OPPORTUNITY_CAP) for o in opportunities)
    return max(0, min(100, 100 - penalty))


def quality_report(
    doc_id: str,
    precision: int = DEFAULT_PRECISION,
    registry: Registry | None = None,
) -> QualityReport:
    """Build the structured quality report for a document (read-only).

    Resolves `doc_id` via the registry; raises `DocumentNotFound` for an unknown id and
    `InspectionError` if the working copy cannot be parsed safely (the tool layer maps both to
    `ToolError`). `precision` is the decimal precision used when counting reducible-coordinate
    opportunities (matching `svg_web_optimize`'s default).
    """
    reg = registry if registry is not None else get_registry()
    try:
        reg.get(doc_id)
    except KeyError:
        raise DocumentNotFound("document id not found") from None

    report = validate_document(doc_id, registry=reg)

    _entry, root = _load_tree(doc_id)
    summary = inspect_summary(doc_id)

    raster_count, raster_bytes = _raster_weight(root)
    viewbox = summary.viewbox
    viewbox_health = ViewBoxHealth(
        present=viewbox is not None,
        valid=viewbox is not None and viewbox[2] > 0 and viewbox[3] > 0,
        width=viewbox[2] if viewbox is not None else None,
        height=viewbox[3] if viewbox is not None else None,
    )

    metrics = QualityMetrics(
        object_count=summary.num_objects,
        node_count=_node_count(root),
        layer_count=summary.num_layers,
        embedded_raster_count=raster_count,
        embedded_raster_bytes=raster_bytes,
        font_coverage=_font_coverage(doc_id),
        viewbox=viewbox_health,
    )

    counts = analyze_optimizations(root, precision)
    opportunities = [
        OptimizationOpportunity(code=code, count=count, message=_OPPORTUNITY_MESSAGES[code])
        for code, count in (
            ("editor_metadata", counts.editor_metadata),
            ("unused_defs", counts.unused_defs),
            ("unreferenced_ids", counts.unreferenced_ids),
            ("empty_groups", counts.empty_groups),
            ("reducible_coords", counts.reducible_coords),
        )
        if count > 0
    ]

    _logger.info(
        "quality report built",
        extra={
            "doc_id": doc_id,
            "ok": report.ok,
            "opportunities": len(opportunities),
        },
    )
    return QualityReport(
        doc_id=doc_id,
        ok=report.ok,
        score=_quality_score(report.error_count, report.warning_count, opportunities),
        findings=report.findings,
        error_count=report.error_count,
        warning_count=report.warning_count,
        metrics=metrics,
        opportunities=opportunities,
    )
