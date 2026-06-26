"""Validation tool: `validate_document`.

Thin MCP layer over the read-only validation engine (`inkscape_mcp.validate`). Direct DOM
only (ADR-005); no mutation and no repair in. All client-facing errors are raised as
`ToolError` with stable, host-path-free messages (fastmcp error model / sec.12).
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.server import mcp
from inkscape_mcp.validate import (
    DocumentNotFound,
    InspectionError,
    ValidationReport,
)
from inkscape_mcp.validate import (
    validate_document as _validate_document,
)

_logger = get_logger("tools.validate")


@mcp.tool
def validate_document(doc_id: str) -> ValidationReport:
    """Validate a loaded document and return structured, machine-readable findings.

    When to use: a pass/fail correctness check on a document. For quantitative metrics + optimize
    opportunities use `quality_report`; to fix size opportunities use `svg_web_optimize`.

    Key params: none beyond `doc_id`.

    Return shape: `ValidationReport` — `ok` (True iff no `error`-severity findings), `error_count`,
    `warning_count`, and `findings` (each a stable machine `code`, a `severity`
    `error`|`warning`|`info`, a human-readable `message`, and an optional `locator`). Covers missing
    fonts, glyph coverage (a `missing_glyphs` warning naming the characters a text element's
    declared font cannot render — read from the font's own cmap, not fontconfig substitution — plus
    a covering family to try), external asset refs, large embedded rasters, id problems (duplicate
    ids / dangling `#id` refs), and viewBox presence/sanity.

    Example: `validate_document(doc_id)`

    Risk class: low (read-only validation; document unchanged).
    """
    try:
        report = _validate_document(doc_id)
    except (DocumentNotFound, KeyError) as exc:
        raise ToolError("document id not found") from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc

    log_tool_call(
        _logger,
        tool="validate_document",
        doc_id=doc_id,
        ok=report.ok,
        error_count=report.error_count,
        warning_count=report.warning_count,
    )
    return report
