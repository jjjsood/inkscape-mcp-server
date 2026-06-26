"""In-process export content-truth verifier tests.

These exercise `inkscape_mcp.render.verify` directly with SYNTHETIC artifacts (hand-built minimal
PDF bytes and Pillow-generated rasters), so they need NO Inkscape binary and run on every host. The
real-engine end-to-end checks (an Inkscape-produced outlined PDF reports vector/outlined; a real PNG
reports a non-zero opaque-pixel count) live in `test_profiles.py` / `test_export.py`.

The verifier MUST compute in-process (no `pdffonts`/`pdfimages`/`mutool` shell-out, no Pillow
subprocess) per sec.12 — these tests assert the verdicts, the supporting code asserts the method.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from inkscape_mcp.render.verify import verify_pdf, verify_raster

# A minimal PDF with NO raster image XObject and NO font object: a single vector path. This is the
# shape an outlined (text-to-path) Inkscape export takes — true vector.
_PDF_VECTOR = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R >>
endobj
4 0 obj
<< /Length 40 >>
stream
10 10 m 90 90 l S
endstream
endobj
%%EOF
"""

# A PDF that embeds a Type1 font (a /Type /Font object + a /FontFile descriptor): NOT outlined.
_PDF_WITH_FONT = b"""%PDF-1.5
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /FontDescriptor 6 0 R >>
endobj
6 0 obj
<< /Type /FontDescriptor /FontName /Helvetica /FontFile 7 0 R >>
endobj
%%EOF
"""

# A PDF that embeds a raster image XObject (/Subtype /Image): NOT true vector.
_PDF_WITH_RASTER = b"""%PDF-1.5
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
8 0 obj
<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /BitsPerComponent 8 >>
stream
endstream
endobj
%%EOF
"""


# --- PDF vector / font verification ------------------------------------------


def test_verify_pdf_vector_reports_true(tmp_path: Path) -> None:
    p = tmp_path / "vector.pdf"
    p.write_bytes(_PDF_VECTOR)
    info = verify_pdf(p)
    assert info is not None
    assert info.is_vector is True
    assert info.fonts_outlined is True


def test_verify_pdf_with_font_reports_not_outlined(tmp_path: Path) -> None:
    p = tmp_path / "font.pdf"
    p.write_bytes(_PDF_WITH_FONT)
    info = verify_pdf(p)
    assert info is not None
    # A PDF with an embedded font is not outlined (fonts_outlined is False); it has no raster, so
    # is_vector stays True — the two facets are independent.
    assert info.fonts_outlined is False
    assert info.is_vector is True


def test_verify_pdf_with_raster_reports_not_vector(tmp_path: Path) -> None:
    p = tmp_path / "raster.pdf"
    p.write_bytes(_PDF_WITH_RASTER)
    info = verify_pdf(p)
    assert info is not None
    assert info.is_vector is False


def test_verify_pdf_handles_spaced_markers(tmp_path: Path) -> None:
    # Tolerates whitespace between key and value: `/Subtype /Image` (spaced) must also be detected.
    p = tmp_path / "spaced.pdf"
    p.write_bytes(b"%PDF-1.5\n<< /Subtype /Image >>\n%%EOF\n")
    info = verify_pdf(p)
    assert info is not None
    assert info.is_vector is False


def test_verify_pdf_non_pdf_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "notpdf.bin"
    p.write_bytes(b"this is not a pdf")
    assert verify_pdf(p) is None


def test_verify_pdf_missing_returns_none(tmp_path: Path) -> None:
    assert verify_pdf(tmp_path / "does_not_exist.pdf") is None


# --- raster opaque-pixel / blank verification --------------------------------


def test_verify_raster_blank_transparent_reports_all_blank(tmp_path: Path) -> None:
    # A fully transparent RGBA canvas: nothing drawn.
    img = Image.new("RGBA", (32, 16), (0, 0, 0, 0))
    p = tmp_path / "blank.png"
    img.save(p)
    info = verify_raster(p)
    assert info is not None
    assert info.opaque_px == 0
    assert info.all_blank is True


def test_verify_raster_drawn_reports_non_zero(tmp_path: Path) -> None:
    # Draw an opaque red square on an otherwise transparent canvas.
    img = Image.new("RGBA", (32, 16), (0, 0, 0, 0))
    for x in range(4, 12):
        for y in range(4, 12):
            img.putpixel((x, y), (255, 0, 0, 255))
    p = tmp_path / "drawn.png"
    img.save(p)
    info = verify_raster(p)
    assert info is not None
    assert info.opaque_px == 64  # an 8x8 opaque block
    assert info.all_blank is False


def test_verify_raster_opaque_flat_reports_all_blank(tmp_path: Path) -> None:
    # An alpha-less raster of a single flat colour is an empty canvas (nothing drawn over it).
    img = Image.new("RGB", (20, 20), (255, 255, 255))
    p = tmp_path / "flat.png"
    img.save(p)
    info = verify_raster(p)
    assert info is not None
    # No alpha: every pixel counts as opaque, but a single flat colour ⇒ blank.
    assert info.opaque_px == 400
    assert info.all_blank is True


def test_verify_raster_opaque_with_content_not_blank(tmp_path: Path) -> None:
    img = Image.new("RGB", (20, 20), (255, 255, 255))
    img.putpixel((5, 5), (0, 0, 0))
    p = tmp_path / "rgb_drawn.png"
    img.save(p)
    info = verify_raster(p)
    assert info is not None
    assert info.opaque_px == 400
    assert info.all_blank is False


def test_verify_raster_bad_file_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "broken.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n not a real png body")
    assert verify_raster(p) is None
