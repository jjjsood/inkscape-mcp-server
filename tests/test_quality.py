"""Quality-report tool + engine tests (E5-05).

Read-only / no Inkscape: `quality_report` extends `validate_document` with metrics and optimization
opportunities. Tests assert the report is a structured model (findings + metrics + opportunities),
the metrics are computed correctly, and the opportunities line up with what `svg_web_optimize`
strips.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.quality import QualityReport
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.server import mcp
from inkscape_mcp.tools.quality import quality_report

# A 1x1 PNG, base64-embedded as a data: raster so embedded-raster weight is non-zero.
_PNG_1x1 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
).decode("ascii")

SVG = (
    b'<?xml version="1.0"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg"'
    b' xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"'
    b' xmlns:xlink="http://www.w3.org/1999/xlink"'
    b' width="100" height="100" viewBox="0 0 100 100">'
    b'<g inkscape:groupmode="layer" id="layer1" inkscape:label="L1">'
    b'<rect id="unrefrect" x="1.234567" y="2" width="3" height="4" fill="#ff0000"/>'
    b'<text id="t1" font-family="No Such Font 42" x="5" y="5">hi</text>'
    b'<image id="img1" x="0" y="0" width="1" height="1" xlink:href="data:image/png;base64,'
    + _PNG_1x1.encode("ascii")
    + b'"/>'
    b"</g>"
    b"</svg>"
)


@pytest.fixture
def doc_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    src = ws / "art.svg"
    src.write_bytes(SVG)
    return get_registry().open_document(str(src)).doc_id


def test_registered_on_mcp(doc_id: str) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "quality_report" in names


def test_returns_structured_model(doc_id: str) -> None:
    report = quality_report(doc_id)
    assert isinstance(report, QualityReport)
    assert report.doc_id == doc_id
    # Wraps validate findings + the metrics block + opportunities.
    assert isinstance(report.findings, list)
    assert report.metrics is not None
    assert isinstance(report.opportunities, list)


def test_metrics_counts_and_viewbox(doc_id: str) -> None:
    m = quality_report(doc_id).metrics
    assert m.object_count >= 3  # rect + text + image (+ layer group)
    assert m.node_count >= 5
    assert m.layer_count == 1
    assert m.embedded_raster_count == 1
    assert m.embedded_raster_bytes > 0
    assert m.viewbox.present and m.viewbox.valid
    assert m.viewbox.width == 100.0 and m.viewbox.height == 100.0


def test_font_coverage_flags_missing(doc_id: str) -> None:
    fc = quality_report(doc_id).metrics.font_coverage
    assert fc.referenced >= 1
    if fc.checked:
        assert "No Such Font 42" in fc.missing


def test_opportunities_align_with_optimizer(doc_id: str) -> None:
    opp = {o.code: o.count for o in quality_report(doc_id).opportunities}
    # Editor cruft (inkscape attrs), an unreferenced id, and reducible coords are all present.
    assert opp.get("editor_metadata", 0) > 0
    assert opp.get("unreferenced_ids", 0) > 0
    assert opp.get("reducible_coords", 0) > 0


def test_unknown_doc_id(doc_id: str) -> None:
    with pytest.raises(ToolError) as exc:
        quality_report("d_missing")
    assert "not found" in str(exc.value)


# --- E13-08: rolled-up 0-100 triage score ---

# No ids (an unreferenced id is itself an optimization opportunity), no cruft, valid viewBox.
_CLEAN_SVG = (
    b'<?xml version="1.0"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
    b'<rect x="0" y="0" width="10" height="10" style="fill:#ff0000"/>'
    b"</svg>"
)


def test_score_penalizes_a_cruft_document(doc_id: str) -> None:
    """The cruft fixture (missing font, unref id, reducible coords) scores below a clean 100."""
    report = quality_report(doc_id)
    assert 0 <= report.score <= 100
    assert report.score < 100  # opportunities + warnings subtract penalties


def test_score_is_100_for_a_clean_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    src = ws / "clean.svg"
    src.write_bytes(_CLEAN_SVG)
    clean_id = get_registry().open_document(str(src)).doc_id

    report = quality_report(clean_id)
    assert report.ok is True
    assert report.score == 100
    assert not report.opportunities
