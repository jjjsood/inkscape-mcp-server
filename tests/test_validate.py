"""Validation tests (E1-08): engine + tool over fixture SVGs.

Covers each finding code (missing_font, external_asset, large_raster, duplicate_id,
missing_id, viewbox_missing, viewbox_invalid), the clean-document happy path, the unknown-id
ToolError, and tool registration on the shared mcp app.
"""

from __future__ import annotations

import asyncio
import base64
import shutil
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.server import mcp
from inkscape_mcp.tools.validate import validate_document as validate_tool
from inkscape_mcp.validate import LARGE_RASTER_BYTES, validate_document

CLEAN_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     width="100" height="50" viewBox="0 0 100 50">
  <rect id="r1" x="0" y="0" width="10" height="10" style="fill:#ff0000"/>
  <circle id="c1" cx="20" cy="20" r="5" style="fill:#00ff00"/>
</svg>
"""

BOGUS_FONT_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     width="100" height="50" viewBox="0 0 100 50">
  <text id="t1" x="5" y="5" font-family="NoSuchFontXYZ123">hello</text>
</svg>
"""

EXTERNAL_ASSET_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:xlink="http://www.w3.org/1999/xlink"
     width="100" height="50" viewBox="0 0 100 50">
  <image id="img1" x="0" y="0" width="10" height="10" xlink:href="external.png"/>
</svg>
"""

DUPLICATE_ID_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     width="100" height="50" viewBox="0 0 100 50">
  <rect id="dup" x="0" y="0" width="10" height="10"/>
  <rect id="dup" x="20" y="0" width="10" height="10"/>
</svg>
"""

MISSING_ID_REF_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     width="100" height="50" viewBox="0 0 100 50">
  <rect id="r1" x="0" y="0" width="10" height="10" fill="url(#nope)"/>
</svg>
"""

NO_VIEWBOX_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50">
  <rect id="r1" x="0" y="0" width="10" height="10"/>
</svg>
"""

INVALID_VIEWBOX_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     width="100" height="50" viewBox="0 0 -5 10">
  <rect id="r1" x="0" y="0" width="10" height="10"/>
</svg>
"""


def _large_raster_svg() -> bytes:
    # Synthesize an embedded PNG data URI whose decoded payload exceeds the threshold.
    blob = base64.b64encode(b"\x00" * (LARGE_RASTER_BYTES + 1024)).decode("ascii")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg"'
        ' xmlns:xlink="http://www.w3.org/1999/xlink"'
        ' width="100" height="50" viewBox="0 0 100 50">\n'
        f'  <image id="big" x="0" y="0" width="10" height="10"'
        f' xlink:href="data:image/png;base64,{blob}"/>\n'
        "</svg>\n"
    ).encode("ascii")


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    return ws


def _open(root: Path, name: str, data: bytes) -> str:
    src = root / name
    src.write_bytes(data)
    return get_registry().open_document(str(src)).doc_id


def test_clean_document_is_ok(root: Path) -> None:
    doc_id = _open(root, "clean.svg", CLEAN_SVG)
    report = validate_document(doc_id)
    assert report.ok is True
    assert report.error_count == 0
    assert not [f for f in report.findings if f.severity == "error"]


def test_missing_font_warning(root: Path) -> None:
    doc_id = _open(root, "font.svg", BOGUS_FONT_SVG)
    report = validate_document(doc_id)
    codes = {f.code for f in report.findings}
    if shutil.which("fc-list") is None:
        # No fontconfig: the check is skipped with an info finding, not a false warning.
        assert "font_check_skipped" in codes
        assert "missing_font" not in codes
    else:
        missing = [f for f in report.findings if f.code == "missing_font"]
        assert any(f.locator == "NoSuchFontXYZ123" for f in missing)
        assert all(f.severity == "warning" for f in missing)


def test_external_asset_warning(root: Path) -> None:
    doc_id = _open(root, "ext.svg", EXTERNAL_ASSET_SVG)
    report = validate_document(doc_id)
    ext = [f for f in report.findings if f.code == "external_asset"]
    assert len(ext) == 1
    assert ext[0].severity == "warning"
    assert ext[0].locator == "external.png"


def test_duplicate_id_error(root: Path) -> None:
    doc_id = _open(root, "dup.svg", DUPLICATE_ID_SVG)
    report = validate_document(doc_id)
    dups = [f for f in report.findings if f.code == "duplicate_id"]
    assert len(dups) == 1
    assert dups[0].severity == "error"
    assert dups[0].locator == "dup"
    assert report.ok is False


def test_missing_id_reference_error(root: Path) -> None:
    doc_id = _open(root, "missref.svg", MISSING_ID_REF_SVG)
    report = validate_document(doc_id)
    missing = [f for f in report.findings if f.code == "missing_id"]
    assert len(missing) == 1
    assert missing[0].severity == "error"
    assert missing[0].locator == "nope"
    assert report.ok is False


def test_viewbox_missing_warning(root: Path) -> None:
    doc_id = _open(root, "novb.svg", NO_VIEWBOX_SVG)
    report = validate_document(doc_id)
    vb = [f for f in report.findings if f.code == "viewbox_missing"]
    assert len(vb) == 1
    assert vb[0].severity == "warning"
    # A missing viewBox is only a warning, so the document is still ok.
    assert report.ok is True


def test_viewbox_invalid_error(root: Path) -> None:
    doc_id = _open(root, "badvb.svg", INVALID_VIEWBOX_SVG)
    report = validate_document(doc_id)
    vb = [f for f in report.findings if f.code == "viewbox_invalid"]
    assert len(vb) == 1
    assert vb[0].severity == "error"
    assert report.ok is False


def test_large_raster_warning(root: Path) -> None:
    doc_id = _open(root, "big.svg", _large_raster_svg())
    report = validate_document(doc_id)
    big = [f for f in report.findings if f.code == "large_raster"]
    assert len(big) == 1
    assert big[0].severity == "warning"
    assert big[0].locator == "big"
    # An oversized raster is only a warning.
    assert report.ok is True


def test_read_only_original_unchanged(root: Path) -> None:
    src = root / "clean.svg"
    src.write_bytes(CLEAN_SVG)
    doc_id = get_registry().open_document(str(src)).doc_id
    validate_document(doc_id)
    assert src.read_bytes() == CLEAN_SVG


def test_tool_unknown_id_raises_toolerror(root: Path) -> None:
    with pytest.raises(ToolError) as exc:
        validate_tool("d_doesnotexist")
    assert "not found" in str(exc.value)


def test_tool_clean_document(root: Path) -> None:
    doc_id = _open(root, "clean.svg", CLEAN_SVG)
    report = validate_tool(doc_id)
    assert report.doc_id == doc_id
    assert report.ok is True


def test_tool_registered_on_mcp(root: Path) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "validate_document" in names


# --- E10-06 V2: validate-vs-quality split (cruft is a quality opportunity, not a defect) ------

# A document carrying optimization "cruft": editor-only metadata, an unreferenced id, and an empty
# group. These are clean-up opportunities, NOT correctness defects.
CRUFT_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
     width="100" height="50" viewBox="0 0 100 50"
     inkscape:version="1.4.3">
  <g id="unused_empty_group"/>
  <rect id="orphan" x="0" y="0" width="10" height="10" style="fill:#ff0000"/>
</svg>
"""

# Cruft / optimization codes that belong to quality_report.opportunities, never validate findings.
_CRUFT_CODES = {
    "editor_metadata",
    "unused_defs",
    "unreferenced_ids",
    "empty_groups",
    "reducible_coords",
}


def test_validate_does_not_emit_cruft_codes(root: Path) -> None:
    """V2: validate_document reports correctness, not cruft — cruft lives in quality_report."""
    doc_id = _open(root, "cruft.svg", CRUFT_SVG)
    report = validate_document(doc_id)
    codes = {f.code for f in report.findings}
    assert codes.isdisjoint(_CRUFT_CODES)
    # The cruft document is correctness-clean (no error findings), confirming cruft is not a defect.
    assert report.ok is True


def test_quality_report_surfaces_cruft_that_validate_omits(root: Path) -> None:
    """The other half of the documented split: quality_report DOES surface cruft opportunities."""
    from inkscape_mcp.quality import quality_report

    doc_id = _open(root, "cruft.svg", CRUFT_SVG)
    opp_codes = {o.code for o in quality_report(doc_id).opportunities}
    # quality_report surfaces cruft (editor metadata, unreferenced id) that validate stays silent on
    assert opp_codes & _CRUFT_CODES
    # And the same document produced no cruft codes in the validation findings.
    val_codes = {f.code for f in validate_document(doc_id).findings}
    assert val_codes.isdisjoint(_CRUFT_CODES)


# --- E13-02: a hostile DOCTYPE / external entity is SURFACED (observable) but never expanded -----

XXE_DOCTYPE_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/hostname">]>
<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="0 0 100 50">
  <text id="leak" x="5" y="5">&xxe;</text>
</svg>
"""

INTERNAL_DOCTYPE_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE svg [<!ENTITY plain "hi">]>
<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="0 0 100 50">
  <rect id="r1" x="0" y="0" width="10" height="10"/>
</svg>
"""


def test_external_entity_surfaced_but_inert(root: Path) -> None:
    """E13-02: a SYSTEM external entity is reported as a warning, but never expanded/leaked."""
    doc_id = _open(root, "xxe.svg", XXE_DOCTYPE_SVG)
    report = validate_document(doc_id)
    ext = [f for f in report.findings if f.code == "external_entity"]
    assert len(ext) == 1
    assert ext[0].severity == "warning"
    assert ext[0].locator == "xxe"
    # The entity is NOT expanded: the host file content never reaches any finding message/locator.
    for finding in report.findings:
        assert "/etc/hostname" not in finding.message
        assert "/etc/hostname" not in (finding.locator or "")
    # An external entity is a risk signal, not a correctness error — `ok` stays True.
    assert report.ok is True


def test_internal_doctype_info_only(root: Path) -> None:
    """A DOCTYPE with only an internal entity yields info `doctype_present`, no external warning."""
    doc_id = _open(root, "doctype.svg", INTERNAL_DOCTYPE_SVG)
    report = validate_document(doc_id)
    codes = {f.code for f in report.findings}
    assert "doctype_present" in codes
    assert "external_entity" not in codes
    info = [f for f in report.findings if f.code == "doctype_present"]
    assert info[0].severity == "info"
    assert report.ok is True


def test_clean_document_has_no_doctype_finding(root: Path) -> None:
    """A normal SVG (no DTD) emits neither doctype finding — the check is opt-in to a DOCTYPE."""
    doc_id = _open(root, "clean.svg", CLEAN_SVG)
    codes = {f.code for f in validate_document(doc_id).findings}
    assert "doctype_present" not in codes
    assert "external_entity" not in codes


# --- E16-04: glyph-coverage / missing-glyph diagnosis -----------------------

# JP (Hiragana こんにちは) text declaring a Latin-only family. The render only "looks right" via
# fontconfig substitution; the saved SVG still names the non-covering family — tofu on a stricter
# renderer. The declared family must be one actually installed on the host (so this is NOT the
# `missing_font` case); the tests probe and skip otherwise.
JP_IN_LATIN_FONT_SVG = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="0 0 100 50">\n'
    '  <text id="jp" x="5" y="20" font-family="Liberation Sans">こんにちは</text>\n'
    "</svg>\n"
).encode()

# Latin text in a Latin family — must validate clean with no missing-glyph false positive, and in
# particular no false flag on the space, comma, exclamation, or parentheses.
LATIN_IN_LATIN_FONT_SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="0 0 100 50">\n'
    b'  <text id="t" x="5" y="20" font-family="Liberation Sans">Hello, World! (123)</text>\n'
    b"</svg>\n"
)


def _font_installed(family: str) -> bool:
    """True iff `family` is genuinely installed (fc-match resolves to it, not a substitute)."""
    from inkscape_mcp.fonts.coverage import family_charset

    return family_charset(family) is not None


def test_missing_glyphs_finding_for_cjk_in_latin_font(root: Path) -> None:
    """JP text in a Latin-only font emits a `missing_glyphs` warning + a covering family."""
    if not _font_installed("Liberation Sans"):
        pytest.skip("Liberation Sans not installed on host")
    doc_id = _open(root, "jp.svg", JP_IN_LATIN_FONT_SVG)
    report = validate_document(doc_id)

    miss = [f for f in report.findings if f.code == "missing_glyphs"]
    assert len(miss) == 1
    finding = miss[0]
    assert finding.severity == "warning"
    assert finding.locator == "jp"
    # The uncovered Hiragana characters are named in the message.
    assert "こんにちは" in finding.message
    # A covering family is suggested (e.g. Noto Sans CJK JP), proving cmap-verified resolution.
    assert "try" in finding.message
    # A missing-glyph problem is a warning, not a hard error — `ok` stays True.
    assert report.ok is True


def test_clean_latin_text_no_missing_glyphs(root: Path) -> None:
    """Latin text in a Latin font produces NO missing-glyph finding (no false positive)."""
    if not _font_installed("Liberation Sans"):
        pytest.skip("Liberation Sans not installed on host")
    doc_id = _open(root, "latin.svg", LATIN_IN_LATIN_FONT_SVG)
    report = validate_document(doc_id)
    assert not [f for f in report.findings if f.code == "missing_glyphs"]


def test_no_text_no_missing_glyphs(root: Path) -> None:
    """A document with no text emits no glyph-coverage finding at all."""
    doc_id = _open(root, "clean.svg", CLEAN_SVG)
    report = validate_document(doc_id)
    assert not [f for f in report.findings if f.code == "missing_glyphs"]
