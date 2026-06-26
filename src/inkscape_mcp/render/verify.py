"""In-process export content-truth verifiers (E16-07).

Pure functions (no MCP decorators, no subprocess) that read a JUST-PRODUCED artifact off disk and
certify what it actually contains, so an export can self-report content-truth rather than the caller
having to shell out to ``pdffonts`` / ``pdfimages`` / ``mutool`` / a Pillow subprocess (sec.12 — no
shell strings; compute in-process).

Two checks:

- **PDF is true vector** (`verify_pdf`): a PDF is reported `is_vector` when it embeds NO raster
  image XObjects (`/Subtype /Image`) and `fonts_outlined` when it embeds NO font objects
  (`/Type /Font` or a `/FontFile*` descriptor). A press/finishing PDF that is BOTH is true vector
  (font-independent, resolution-independent). Computed by a minimal byte scan of the PDF — no PDF
  library is pulled in; the markers we look for are ASCII tokens in the object dictionaries
  Inkscape emits. The scan is bounded and tolerant: an unreadable/short file yields `None`
  (unknown), never an error.

- **Raster actually drew pixels** (`verify_raster`): a PNG/raster is summarized by `opaque_px` (the
  count of non-fully-transparent pixels) and `all_blank` (True iff nothing was drawn — every pixel
  fully transparent, OR, for an alpha-less raster, a single flat colour, i.e. an empty canvas).
  Computed with Pillow as a LIBRARY (already a project dependency — `pillow>=10.0`), never a Pillow
  subprocess. A decode failure yields `None` (unknown), never an error.

SECURITY (sec.12): every input path here is a SERVER-MINTED artifact path that the render/export
engine just wrote under the sandbox (never client input); these functions only READ it. No host path
is ever surfaced — the callers fold the booleans/counts into the existing host-path-free result
models.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from inkscape_mcp.logging_setup import get_logger

_logger = get_logger("render.verify")

#: Cap on PDF bytes scanned for the vector/font markers. A real Inkscape PDF export is comfortably
#: under this; the cap bounds the work for a pathologically large file (defense in depth).
_PDF_SCAN_MAX_BYTES = 64 * 1024 * 1024

#: Raster image XObject marker — present iff the PDF embeds at least one raster image.
_IMAGE_MARKERS: tuple[bytes, ...] = (b"/Subtype/Image", b"/Subtype /Image")

#: Embedded-font markers — present iff the PDF carries a font object or an embedded font program.
#: A truly outlined (text-to-path) PDF carries NONE of these.
_FONT_MARKERS: tuple[bytes, ...] = (
    b"/Type/Font",
    b"/Type /Font",
    b"/FontFile",  # covers /FontFile, /FontFile2, /FontFile3 (embedded font programs)
)


class PdfVectorInfo(BaseModel):
    """Content-truth summary of a produced PDF (E16-07).

    `is_vector` is True iff the PDF embeds NO raster image XObjects; `fonts_outlined` is True iff it
    embeds NO font objects (text was outlined to paths). A PDF that is BOTH is true vector — safe
    for press/finishing with no font or resolution dependency. `verify_pdf` returns `None` instead
    when the file cannot be read, so a caller never sees a misreported verdict.
    """

    is_vector: bool
    fonts_outlined: bool


class RasterContentInfo(BaseModel):
    """Content-truth summary of a produced raster (E16-07).

    `opaque_px` is the count of non-fully-transparent pixels actually drawn; `all_blank` is True iff
    nothing was drawn (a fully transparent raster, or — for an alpha-less raster — a single flat
    colour, i.e. an empty canvas). Lets a caller prove a render is not blank straight from the
    result, without re-reading the file.
    """

    opaque_px: int
    all_blank: bool


def verify_pdf(path: Path) -> PdfVectorInfo | None:
    """Scan a produced PDF in-process for raster-image and embedded-font markers (E16-07).

    Returns `PdfVectorInfo(is_vector, fonts_outlined)` — `is_vector` when no `/Subtype /Image`
    XObject is present, `fonts_outlined` when no `/Type /Font` / `/FontFile*` marker is present.
    Reads at most `_PDF_SCAN_MAX_BYTES` and tolerates whitespace between the key and value
    (`/Subtype/Image` and `/Subtype /Image` both match). Returns `None` (unknown) if the file is
    missing/unreadable — the caller then omits the verdict rather than misreporting. No subprocess,
    no PDF dependency, host-path-free.
    """
    try:
        with path.open("rb") as fh:
            data = fh.read(_PDF_SCAN_MAX_BYTES)
    except OSError:  # pragma: no cover - the export just wrote this file
        return None
    if not data.startswith(b"%PDF"):
        return None
    has_raster = any(marker in data for marker in _IMAGE_MARKERS)
    has_font = any(marker in data for marker in _FONT_MARKERS)
    return PdfVectorInfo(is_vector=not has_raster, fonts_outlined=not has_font)


def verify_raster(path: Path) -> RasterContentInfo | None:
    """Count the drawn (non-transparent) pixels of a produced raster in-process via Pillow (E16-07).

    Returns `RasterContentInfo(opaque_px, all_blank)`. With an alpha channel, `opaque_px` is the
    number of pixels whose alpha is non-zero and `all_blank` is True iff that count is zero. Without
    an alpha channel (an opaque raster) every pixel counts as drawn, so `opaque_px` is the full
    pixel count and `all_blank` is True only iff the whole image is a single flat colour (an empty
    canvas). Uses Pillow as a LIBRARY (a project dependency), never a subprocess. Returns `None`
    (unknown) on a decode failure. Host-path-free.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - pillow is a hard dependency
        return None
    try:
        with Image.open(path) as im:
            im.load()
            total = im.width * im.height
            if "A" in im.getbands():
                alpha = im.getchannel("A")
                histogram = alpha.histogram()
                # Pixels with alpha 0 are fully transparent (not drawn); the rest are drawn.
                transparent = histogram[0] if histogram else 0
                opaque = total - transparent
                return RasterContentInfo(opaque_px=opaque, all_blank=opaque == 0)
            # No alpha channel: every pixel is opaque. "Blank" then means a single flat colour
            # across the whole canvas (nothing was drawn over the background). `getcolors` returns
            # one entry per distinct colour (or None when the image has more than `maxcolors`); a
            # single distinct colour ⇒ a flat, empty canvas.
            colours = im.getcolors(maxcolors=2)
            flat = colours is not None and len(colours) <= 1
            return RasterContentInfo(opaque_px=total, all_blank=flat)
    except (OSError, ValueError) as exc:
        _logger.warning("raster verify failed", extra={"error": type(exc).__name__})
        return None
