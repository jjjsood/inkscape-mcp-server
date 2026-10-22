"""`validate_document` engine: one case per finding code, the ok/severity contract, DOCTYPE
handling, glyph coverage, and the validate-vs-quality split (cruft is an opportunity, not a defect).
"""

from __future__ import annotations

import base64
import shutil

import pytest

from inkscape_mcp.quality import quality_report
from inkscape_mcp.validate import LARGE_RASTER_BYTES, validate_document
from tests.tool_support import open_doc

_NS = 'xmlns="http://www.w3.org/2000/svg"'
_NS_X = _NS + ' xmlns:xlink="http://www.w3.org/1999/xlink"'


def _svg(body: str, *, viewbox: str | None = "0 0 100 50", head: str = "", ns: str = _NS) -> bytes:
    vb = f' viewBox="{viewbox}"' if viewbox else ""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n{head}<svg {ns} width="100" height="50"{vb}>'
        f"{body}</svg>"
    ).encode()


CLEAN = _svg(
    '<rect id="r1" x="0" y="0" width="10" height="10" style="fill:#ff0000"/>'
    '<circle id="c1" cx="20" cy="20" r="5" style="fill:#00ff00"/>'
)
_BIG = base64.b64encode(b"\x00" * (LARGE_RASTER_BYTES + 1024)).decode("ascii")


def _codes(report, code: str):
    return [f for f in report.findings if f.code == code]


# (svg, finding code, severity, locator, report.ok)
CASES = {
    "external_asset": (
        _svg('<image id="img1" width="10" height="10" xlink:href="external.png"/>', ns=_NS_X),
        "external_asset",
        "warning",
        "external.png",
        True,
    ),
    "duplicate_id": (
        _svg(
            '<rect id="dup" width="10" height="10"/><rect id="dup" x="20" width="10" height="10"/>'
        ),
        "duplicate_id",
        "error",
        "dup",
        False,
    ),
    "missing_id": (
        _svg('<rect id="r1" width="10" height="10" fill="url(#nope)"/>'),
        "missing_id",
        "error",
        "nope",
        False,
    ),
    "viewbox_missing": (
        _svg('<rect id="r1" width="10" height="10"/>', viewbox=None),
        "viewbox_missing",
        "warning",
        None,
        True,
    ),
    "viewbox_invalid": (
        _svg('<rect id="r1" width="10" height="10"/>', viewbox="0 0 -5 10"),
        "viewbox_invalid",
        "error",
        None,
        False,
    ),
    "large_raster": (
        _svg(
            f'<image id="big" width="10" height="10" xlink:href="data:image/png;base64,{_BIG}"/>',
            ns=_NS_X,
        ),
        "large_raster",
        "warning",
        "big",
        True,
    ),
}


def test_clean_document_is_ok(tool_ws) -> None:
    report = validate_document(open_doc(tool_ws, "clean.svg", CLEAN))
    assert report.ok is True and report.error_count == 0
    assert not [f for f in report.findings if f.severity == "error"]


@pytest.mark.parametrize("name", sorted(CASES))
def test_each_finding_code(tool_ws, name: str) -> None:
    data, code, severity, locator, ok = CASES[name]
    report = validate_document(open_doc(tool_ws, f"{name}.svg", data))
    (finding,) = _codes(report, code)
    assert finding.severity == severity
    if locator is not None:
        assert finding.locator == locator
    assert report.ok is ok  # only error-severity findings make a document not ok


def test_missing_font_warning_or_skipped_without_fontconfig(tool_ws) -> None:
    data = _svg('<text id="t1" x="5" y="5" font-family="NoSuchFontXYZ123">hello</text>')
    report = validate_document(open_doc(tool_ws, "font.svg", data))
    codes = {f.code for f in report.findings}
    if shutil.which("fc-list") is None:
        # No fontconfig: the check is skipped with an info finding, not a false warning.
        assert "font_check_skipped" in codes and "missing_font" not in codes
    else:
        missing = _codes(report, "missing_font")
        assert any(f.locator == "NoSuchFontXYZ123" for f in missing)
        assert all(f.severity == "warning" for f in missing)


def test_validation_is_read_only(tool_ws) -> None:
    doc = open_doc(tool_ws, "clean.svg", CLEAN)
    validate_document(doc)
    assert (tool_ws / "clean.svg").read_bytes() == CLEAN


# --- validate vs quality: cruft is a quality opportunity, never a validation finding ---------

CRUFT = _svg(
    '<g id="unused_empty_group"/><rect id="orphan" width="10" height="10" style="fill:#f00"/>',
    ns=_NS + ' xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" inkscape:version="1.4"',
)
CRUFT_CODES = {
    "editor_metadata",
    "unused_defs",
    "unreferenced_ids",
    "empty_groups",
    "reducible_coords",
}


def test_cruft_is_a_quality_opportunity_not_a_defect(tool_ws) -> None:
    doc = open_doc(tool_ws, "cruft.svg", CRUFT)
    report = validate_document(doc)
    assert {f.code for f in report.findings}.isdisjoint(CRUFT_CODES)
    assert report.ok is True
    assert {o.code for o in quality_report(doc).opportunities} & CRUFT_CODES


# --- hostile DOCTYPE: surfaced but never expanded ----------------------------------------------


def test_external_entity_is_surfaced_but_inert(tool_ws) -> None:
    data = _svg(
        '<text id="leak" x="5" y="5">&xxe;</text>',
        head='<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/hostname">]>\n',
    )
    report = validate_document(open_doc(tool_ws, "xxe.svg", data))
    (ext,) = _codes(report, "external_entity")
    assert (ext.severity, ext.locator) == ("warning", "xxe")
    for finding in report.findings:  # the host file content never reaches a finding
        assert "/etc/hostname" not in finding.message + (finding.locator or "")
    assert report.ok is True  # a risk signal, not a correctness error


def test_internal_doctype_is_info_only(tool_ws) -> None:
    data = _svg(
        '<rect id="r1" width="10" height="10"/>', head='<!DOCTYPE svg [<!ENTITY plain "hi">]>\n'
    )
    report = validate_document(open_doc(tool_ws, "doctype.svg", data))
    (info,) = _codes(report, "doctype_present")
    assert info.severity == "info"
    assert not _codes(report, "external_entity") and report.ok is True


def test_no_doctype_means_no_doctype_findings(tool_ws) -> None:
    codes = {f.code for f in validate_document(open_doc(tool_ws, "clean.svg", CLEAN)).findings}
    assert not codes & {"doctype_present", "external_entity", "missing_glyphs"}


# --- glyph coverage ----------------------------------------------------------------------------


def _font_installed(family: str) -> bool:
    from inkscape_mcp.fonts.coverage import family_charset

    return family_charset(family) is not None


def _text(content: str) -> bytes:
    return _svg(f'<text id="t" x="5" y="20" font-family="Liberation Sans">{content}</text>')


def test_cjk_in_a_latin_font_reports_missing_glyphs(tool_ws) -> None:
    from inkscape_mcp.fonts.coverage import suggest_covering_family

    if not _font_installed("Liberation Sans"):
        pytest.skip("Liberation Sans not installed on host")
    if suggest_covering_family("こんにちは") is None:
        pytest.skip("no CJK-covering font installed on host to suggest")
    report = validate_document(open_doc(tool_ws, "jp.svg", _text("こんにちは")))
    (finding,) = _codes(report, "missing_glyphs")
    assert (finding.severity, finding.locator) == ("warning", "t")
    assert "こんにちは" in finding.message and "try" in finding.message  # names a covering family
    assert report.ok is True


def test_latin_text_in_a_latin_font_has_no_false_positive(tool_ws) -> None:
    if not _font_installed("Liberation Sans"):
        pytest.skip("Liberation Sans not installed on host")
    report = validate_document(open_doc(tool_ws, "latin.svg", _text("Hello, World! (123)")))
    assert not _codes(report, "missing_glyphs")
