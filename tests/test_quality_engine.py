"""`quality_report` engine, `inspect(include=[quality])`, and the cross-document consistency verdict
(`inspect(doc_id=[...], include=[quality])` replaced the old `quality_report_set`).
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.quality import QualityReport, build_set_verdict, quality_report
from inkscape_mcp.registry import get_registry
from inkscape_mcp.workspace import sandbox
from tests.tool_support import call, open_doc

# A 1x1 PNG embedded as a data: raster so the embedded-raster weight is non-zero.
_PNG = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
).decode("ascii")

CRUFTY = (
    '<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"'
    ' xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"'
    ' xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="100" viewBox="0 0 100 100">'
    '<g inkscape:groupmode="layer" id="layer1" inkscape:label="L1">'
    '<rect id="unrefrect" x="1.234567" y="2" width="3" height="4" fill="#ff0000"/>'
    '<text id="t1" font-family="No Such Font 42" x="5" y="5">hi</text>'
    f'<image id="img1" width="1" height="1" xlink:href="data:image/png;base64,{_PNG}"/>'
    "</g></svg>"
).encode()

# No ids (an unreferenced id is itself an opportunity), no cruft, valid viewBox.
CLEAN = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
    b'<rect x="0" y="0" width="10" height="10" style="fill:#ff0000"/></svg>'
)


@pytest.fixture
def crufty(tool_ws) -> str:
    return open_doc(tool_ws, "art.svg", CRUFTY)


def test_report_is_a_structured_model(crufty) -> None:
    report = quality_report(crufty)
    assert isinstance(report, QualityReport) and report.doc_id == crufty
    assert report.metrics is not None and isinstance(report.findings, list)


def test_metrics_counts_and_viewbox(crufty) -> None:
    m = quality_report(crufty).metrics
    assert m.object_count >= 3 and m.node_count >= 5 and m.layer_count == 1
    assert m.embedded_raster_count == 1 and m.embedded_raster_bytes > 0
    assert (m.viewbox.present, m.viewbox.valid) == (True, True)
    assert (m.viewbox.width, m.viewbox.height) == (100.0, 100.0)


def test_font_coverage_flags_the_missing_family(crufty) -> None:
    fc = quality_report(crufty).metrics.font_coverage
    assert fc.referenced >= 1
    if fc.checked:  # skipped, not "all missing", when fontconfig is unavailable
        assert "No Such Font 42" in fc.missing


def test_opportunities_align_with_the_optimizer(crufty) -> None:
    opp = {o.code: o.count for o in quality_report(crufty).opportunities}
    for code in ("editor_metadata", "unreferenced_ids", "reducible_coords"):
        assert opp.get(code, 0) > 0


def test_score_penalizes_cruft_and_is_100_when_clean(crufty, tool_ws) -> None:
    assert 0 <= quality_report(crufty).score < 100
    clean = quality_report(open_doc(tool_ws, "clean.svg", CLEAN))
    assert clean.ok is True and clean.score == 100 and not clean.opportunities


# --- through the `inspect` tool ----------------------------------------------------------------


def test_tool_returns_the_typed_report_without_mutating(tool_app, tool_ws, crufty) -> None:
    root = Path(get_registry().get(crufty).root)
    before = sandbox.working_copy(root, crufty).read_bytes()

    q = call(tool_app, "inspect", {"doc_id": crufty, "include": ["quality"]}).data["quality"]

    assert q["doc_id"] == crufty
    assert {"object_count", "node_count", "layer_count", "font_coverage", "viewbox"} <= set(
        q["metrics"]
    )
    assert all({"code", "count", "message"} <= set(o) for o in q["opportunities"])
    assert {"editor_metadata", "unreferenced_ids", "reducible_coords"} <= {
        o["code"] for o in q["opportunities"]
    }
    # read-only: same bytes, and no snapshot or operation record was written
    assert sandbox.working_copy(root, crufty).read_bytes() == before
    ops = sandbox.operations_dir(root, crufty)
    assert not ops.exists() or not any(ops.iterdir())
    assert (
        "snapshots"
        not in call(tool_app, "inspect", {"doc_id": crufty, "include": ["snapshots"]}).data
    )


def test_tool_refuses_an_unknown_document(tool_app, tool_ws) -> None:
    with pytest.raises(ToolError, match="not found"):
        call(tool_app, "inspect", {"doc_id": "d_missing", "include": ["quality"]})


# --- collection set: per-doc quality + cross-document consistency verdict -----------------------


def _asset(oid: str, fill: str, *, size: int = 100, stroke: int = 2) -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}"><rect id="{oid}" x="10" y="10" width="40" height="40" '
        f'stroke="#000" stroke-width="{stroke}" fill="{fill}"/></svg>'
    ).encode()


@pytest.fixture
def consistent(tool_ws) -> list[str]:
    return [
        open_doc(tool_ws, f"{n}.svg", _asset(f"icon_{n}", c))
        for n, c in (("a", "#f00"), ("b", "#0f0"), ("c", "#00f"))
    ]


@pytest.fixture
def odd(tool_ws) -> str:
    """Disagrees with `consistent` on viewBox (200x200) and stroke-width (5)."""
    return open_doc(tool_ws, "odd.svg", _asset("odd_box", "#00f", size=200, stroke=5))


def _verdict(app, docs):
    data = call(app, "inspect", {"doc_id": docs, "include": ["quality"]}).data
    return data, {p["property"]: p for p in data["consistency"].get("properties", [])}


def test_consistent_set(tool_app, consistent) -> None:
    data, props = _verdict(tool_app, consistent)
    assert set(data["docs"]) == set(consistent)
    assert all("quality" in sections for sections in data["docs"].values())
    assert data["consistency"]["consistent"] is True
    assert props and all(p["agree"] for p in props.values())


def test_set_is_read_only(tool_app, consistent) -> None:
    _verdict(tool_app, consistent)
    for doc in consistent:
        root = Path(get_registry().get(doc).root)
        snaps = sandbox.snapshots_dir(root, doc)
        assert not snaps.exists() or not list(snaps.glob("*.svg"))
        ops = sandbox.operations_dir(root, doc)
        assert not ops.exists() or not list(ops.glob("op_*.json"))


def test_set_flags_the_disagreeing_document(tool_app, consistent, odd) -> None:
    data, props = _verdict(tool_app, [*consistent, odd])
    assert data["consistency"]["consistent"] is False

    viewbox = props["viewBox"]
    assert viewbox["agree"] is False
    odd_bucket = next(ids for val, ids in viewbox["values"].items() if "200" in val)
    assert odd_bucket == [odd]
    assert set(viewbox["values"]["100x100"]) == set(consistent)

    stroke = props["stroke_width"]
    assert stroke["agree"] is False and stroke["values"]["5"] == [odd]


def test_verdict_engine_matches_the_tool(tool_app, consistent, odd) -> None:
    docs = [*consistent, odd]
    verdict = build_set_verdict(docs)
    assert verdict.consistent is False
    assert {p.property for p in verdict.properties if not p.agree} >= {"viewBox", "stroke_width"}
    assert verdict.model_dump(mode="json", exclude_none=True)["consistent"] is False


def test_set_without_quality_has_no_verdict(tool_app, consistent) -> None:
    data = call(tool_app, "inspect", {"doc_id": consistent}).data
    assert set(data["docs"]) == set(consistent) and "consistency" not in data


def test_set_rejects_empty_duplicate_and_unknown(tool_app, consistent) -> None:
    with pytest.raises(ToolError, match="at least one"):
        call(tool_app, "inspect", {"doc_id": [], "include": ["quality"]})
    with pytest.raises(ToolError, match="duplicate"):
        call(tool_app, "inspect", {"doc_id": [consistent[0]] * 2, "include": ["quality"]})
    with pytest.raises(ToolError, match="document id not found"):
        call(tool_app, "inspect", {"doc_id": [consistent[0], "nope"], "include": ["quality"]})
