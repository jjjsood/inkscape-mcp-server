"""Export-profile tool tests: export_web_profile / create_icon_set / export_print_profile.

Following the `tests/test_export.py` pattern: Inkscape-dependent tests are marked
`@pytest.mark.inkscape` and actually run the local binary (it IS available on this host); they
skip when it is absent. The size-rejection, unknown-doc, and registration tests need no Inkscape
(validation / lookup happens before any invocation).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render.profiles import DEFAULT_ICON_SIZES
from inkscape_mcp.server import mcp
from inkscape_mcp.tools.profiles import (
    create_icon_set,
    export_print_profile,
    export_web_profile,
)

SVG = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
     width="100" height="80" viewBox="0 0 100 80">
  <rect id="bg" x="0" y="0" width="100" height="80" fill="#ffffff"/>
  <circle id="dot" cx="30" cy="30" r="20" fill="#ff0000"/>
  <rect id="box" x="55" y="10" width="30" height="40" fill="#0000ff"/>
</svg>
"""

PNG_MAGIC = b"\x89PNG"
PDF_MAGIC = b"%PDF"

inkscape_available = shutil.which("inkscape") is not None


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    return ws


@pytest.fixture
def doc_id(root: Path) -> str:
    src = root / "art.svg"
    src.write_bytes(SVG)
    return get_registry().open_document(str(src)).doc_id


def _root_join(doc_id: str, ws_rel: str) -> Path:
    """Join a returned path (`path` or `workspace_relative_path`) to the workspace ROOT.

    ONE LOCATION CONTRACT: both fields are root-relative and identical, so a single plain join to
    the workspace ROOT opens any artifact — no `find`/`stat`.
    """
    entry = get_registry().get(doc_id)
    return Path(entry.root) / ws_rel


# --- registration (no Inkscape) ---------------------------------------------


def test_tools_registered_on_mcp(root: Path) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {"export_web_profile", "create_icon_set", "export_print_profile"} <= names


# --- icon-set size rejection (no Inkscape; validation happens first) --------


def test_create_icon_set_rejects_oversized(doc_id: str) -> None:
    too_big = get_settings().max_export_px + 1
    with pytest.raises(ToolError) as exc:
        create_icon_set(doc_id, sizes=[64, too_big])
    # PF5: over-cap cause is its own message, distinct from the <=0 case.
    assert "exceeds the configured pixel cap" in str(exc.value)


@pytest.mark.parametrize("bad_size", [0, -1, -256])
def test_create_icon_set_rejects_non_positive(doc_id: str, bad_size: int) -> None:
    with pytest.raises(ToolError) as exc:
        create_icon_set(doc_id, sizes=[bad_size])
    # PF5: <=0 size gets its OWN distinguishable message, not the over-cap "limit" one.
    assert "must be a positive integer" in str(exc.value)


def test_create_icon_set_message_split_distinguishes_cause(doc_id: str) -> None:
    # PF5: 999999 (over-cap) and 0 (<=0) previously returned the SAME message; now distinct.
    too_big = get_settings().max_export_px + 1
    with pytest.raises(ToolError) as over:
        create_icon_set(doc_id, sizes=[too_big])
    with pytest.raises(ToolError) as nonpos:
        create_icon_set(doc_id, sizes=[0])
    assert str(over.value) != str(nonpos.value)


# --- unknown doc (no Inkscape) ----------------------------------------------


def test_unknown_doc_id_web(root: Path) -> None:
    with pytest.raises(ToolError) as exc:
        export_web_profile("d_missing")
    assert "not found" in str(exc.value)


def test_unknown_doc_id_print(root: Path) -> None:
    with pytest.raises(ToolError) as exc:
        export_print_profile("d_missing")
    assert "not found" in str(exc.value)


# --- web profile -------------------------------------------------------------


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_web_profile_produces_png_and_svg(doc_id: str) -> None:
    result = export_web_profile(doc_id, width_px=256)
    assert result.profile == "web"
    # Fixed, ordered scheme: PNG first, then SVG.
    formats = [a.format for a in result.artifacts]
    assert formats == ["png", "svg"]

    png, svg = result.artifacts
    assert png.width_px == 256
    # height follows the 100x80 aspect ratio
    assert png.height_px == 205 or png.height_px == 204
    for art in result.artifacts:
        # ONE LOCATION CONTRACT: path == workspace_relative_path, both root-relative.
        assert not art.path.startswith("/")
        assert art.path == art.workspace_relative_path
        out = _root_join(doc_id, art.path)
        assert out.exists()

    png_out = _root_join(doc_id, png.path)
    assert png_out.read_bytes()[:4] == PNG_MAGIC
    svg_head = _root_join(doc_id, svg.path).read_bytes()[:64].lstrip()
    assert svg_head.startswith(b"<?xml") or svg_head.startswith(b"<svg")


# --- icon set ----------------------------------------------------------------


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_create_icon_set_default_sizes(doc_id: str) -> None:
    result = create_icon_set(doc_id)
    assert result.profile == "icon"
    # One artifact per default size, in order.
    assert [a.requested_size_px for a in result.artifacts] == list(DEFAULT_ICON_SIZES)
    for art in result.artifacts:
        assert art.format == "png"
        assert art.width_px == art.requested_size_px
        assert not art.path.startswith("/")  # workspace-relative
        out = _root_join(doc_id, art.path)
        assert out.exists()
        assert out.read_bytes()[:4] == PNG_MAGIC


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_create_icon_set_custom_sizes(doc_id: str) -> None:
    result = create_icon_set(doc_id, sizes=[24, 96])
    assert [a.requested_size_px for a in result.artifacts] == [24, 96]
    assert [a.width_px for a in result.artifacts] == [24, 96]


# --- print profile -----------------------------------------------------------


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_print_profile_produces_pdf(doc_id: str) -> None:
    result = export_print_profile(doc_id)
    assert result.profile == "print"
    assert len(result.artifacts) == 1
    pdf = result.artifacts[0]
    assert pdf.format == "pdf"
    assert pdf.path.endswith(".pdf")
    assert not pdf.path.startswith("/")  # workspace-relative
    # vector output: no raster dimensions
    assert pdf.width_px is None
    assert pdf.height_px is None
    out = _root_join(doc_id, pdf.path)
    assert out.exists()
    assert out.read_bytes()[:4] == PDF_MAGIC


# ---: resolvable locations -------------------------------------------


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_web_profile_locations_resolve_by_root_join(doc_id: str) -> None:
    # ONE LOCATION CONTRACT: every emitted file's `path` == `workspace_relative_path`, both
    # root-relative, and opens by a single plain join to the workspace ROOT (no find/stat).
    result = export_web_profile(doc_id, width_px=128)
    for art in result.artifacts:
        assert art.path == art.workspace_relative_path
        assert art.path.startswith(f".inkscape-mcp/documents/{doc_id}/")
        assert _root_join(doc_id, art.path).exists()
        assert _root_join(doc_id, art.workspace_relative_path).exists()


# ---: responsive web set ---------------------------------------------


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_web_profile_responsive_scales(doc_id: str) -> None:
    # One call emits the full 1x/2x/3x responsive PNG set plus the SVG — each file distinct and
    # resolvable; entries report their density scale.
    result = export_web_profile(doc_id, width_px=100, scales=[1, 2, 3])
    pngs = [a for a in result.artifacts if a.format == "png"]
    svgs = [a for a in result.artifacts if a.format == "svg"]
    assert len(pngs) == 3
    assert len(svgs) == 1
    assert sorted(a.width_px for a in pngs) == [100, 200, 300]
    assert sorted(a.scale or 0 for a in pngs) == [1, 2, 3]
    # All PNGs distinct on disk and resolvable.
    paths = {a.workspace_relative_path for a in pngs}
    assert len(paths) == 3
    for a in pngs:
        assert _root_join(doc_id, a.workspace_relative_path).exists()


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_web_profile_explicit_widths(doc_id: str) -> None:
    result = export_web_profile(doc_id, widths=[64, 256])
    pngs = [a for a in result.artifacts if a.format == "png"]
    assert sorted(a.width_px for a in pngs) == [64, 256]
    assert len({a.workspace_relative_path for a in pngs}) == 2
    #: widths-mode entries carry no density `scale` but DO carry `requested_width_px`, so a
    # caller can always identify which requested width each responsive entry corresponds to.
    assert all(a.scale is None for a in pngs)
    assert sorted(a.requested_width_px or 0 for a in pngs) == [64, 256]


@pytest.mark.parametrize("bad", [[0], [-5]])
def test_web_profile_rejects_non_positive_width(doc_id: str, bad: list[int]) -> None:
    with pytest.raises(ToolError) as exc:
        export_web_profile(doc_id, widths=bad)
    assert "positive integer" in str(exc.value)


def test_web_profile_rejects_non_positive_scale(doc_id: str) -> None:
    with pytest.raises(ToolError) as exc:
        export_web_profile(doc_id, width_px=100, scales=[0])
    assert "positive integer" in str(exc.value)


# ---: print profile applies + reports real settings ------------------


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_print_profile_reports_applied_settings(doc_id: str) -> None:
    # The print profile reports the print-specific options it applied (auditable).
    result = export_print_profile(doc_id)
    assert result.applied_settings.get("text_to_path") == "true"
    # Pinned to 1.4 (vs Inkscape's default 1.5) so the print output ALWAYS differs from a plain PDF.
    assert result.applied_settings.get("pdf_version") == "1.4"


# A text-bearing document: --export-text-to-path outlines the text, so the print profile's PDF
# bytes observably differ from a plain PDF export (S3).
SVG_TEXT = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="80" viewBox="0 0 200 80">
  <rect x="0" y="0" width="200" height="80" fill="#ffffff"/>
  <text x="10" y="45" font-size="20" fill="#000000">Print Me</text>
</svg>
"""


@pytest.fixture
def text_doc_id(root: Path) -> str:
    src = root / "text.svg"
    src.write_bytes(SVG_TEXT)
    return get_registry().open_document(str(src)).doc_id


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_print_profile_bytes_differ_from_plain_pdf(text_doc_id: str) -> None:
    # S3: with text present, the print profile's text-to-path flag changes the PDF bytes
    # vs a plain PDF export, so the print value is observable (not byte-identical).
    from inkscape_mcp.tools.export import export_document as _plain_export

    plain = _plain_export(text_doc_id, "pdf")
    profile = export_print_profile(text_doc_id)
    plain_bytes = _root_join(text_doc_id, plain.workspace_relative_path).read_bytes()
    pdf = profile.artifacts[0]
    profile_bytes = _root_join(text_doc_id, pdf.workspace_relative_path).read_bytes()
    assert plain_bytes != profile_bytes


# A TEXT-FREE document (only shapes): --export-text-to-path is a no-op here, so the OBSERVABLE
# print difference must come from the pinned PDF version (1.4 vs the plain export's default 1.5).
SVG_TEXTFREE = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" width="120" height="90" viewBox="0 0 120 90">
  <rect x="0" y="0" width="120" height="90" fill="#ffffff"/>
  <circle cx="40" cy="45" r="30" fill="#1166cc"/>
  <rect x="80" y="20" width="30" height="50" fill="#cc2211"/>
</svg>
"""


@pytest.fixture
def textfree_doc_id(root: Path) -> str:
    src = root / "shapes.svg"
    src.write_bytes(SVG_TEXTFREE)
    return get_registry().open_document(str(src)).doc_id


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_print_profile_bytes_differ_from_plain_pdf_textfree(textfree_doc_id: str) -> None:
    #: the print profile must produce DIFFERENT bytes than a plain PDF export even when the
    # document has NO text. The pinned PDF version 1.4 (vs the plain export's default 1.5)
    # guarantees this — the file header (`%PDF-1.4` vs `%PDF-1.5`) is a deterministic,
    # content-independent byte difference.
    from inkscape_mcp.tools.export import export_document as _plain_export

    plain = _plain_export(textfree_doc_id, "pdf")
    profile = export_print_profile(textfree_doc_id)
    plain_bytes = _root_join(textfree_doc_id, plain.workspace_relative_path).read_bytes()
    pdf = profile.artifacts[0]
    profile_bytes = _root_join(textfree_doc_id, pdf.workspace_relative_path).read_bytes()
    assert plain_bytes != profile_bytes
    # The byte difference is observable right at the header (version pin).
    assert plain_bytes[:8] == b"%PDF-1.5"
    assert profile_bytes[:8] == b"%PDF-1.4"


# ---: capability-absent profile errors name list_capabilities ---------


def test_profile_map_failure_missing_engine_names_list_capabilities() -> None:
    from inkscape_mcp.tools.profiles import _map_failure
    from inkscape_mcp.workspace.subprocess_exec import ProcessError

    msg = str(_map_failure(ProcessError("inkscape binary not found")))
    assert "list_capabilities" in msg
    assert "binary not found" not in msg  # host-path/detail-free (sec.12)


# ---: profile exporters honor out_dir / name_prefix ------------------


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_web_profile_out_dir_in_workspace(root: Path, doc_id: str) -> None:
    # A caller-chosen, in-workspace out_dir is honored: every artifact lands under <root>/dist/web/
    # and resolves by a single root join.
    result = export_web_profile(doc_id, width_px=64, out_dir="dist/web", name_prefix="brand")
    assert result.artifacts
    for art in result.artifacts:
        assert art.path == art.workspace_relative_path
        assert art.path.startswith("dist/web/")
        assert "brand" in Path(art.path).name
        out = _root_join(doc_id, art.path)
        assert out.exists()


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_icon_set_out_dir_in_workspace(root: Path, doc_id: str) -> None:
    result = create_icon_set(doc_id, sizes=[16, 32], out_dir="dist/icons", name_prefix="app")
    assert [a.requested_size_px for a in result.artifacts] == [16, 32]
    for art in result.artifacts:
        assert art.path.startswith("dist/icons/")
        assert "app" in Path(art.path).name
        assert _root_join(doc_id, art.path).exists()


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_print_profile_out_dir_in_workspace(root: Path, doc_id: str) -> None:
    result = export_print_profile(doc_id, out_dir="dist/print", name_prefix="final")
    pdf = result.artifacts[0]
    assert pdf.path.startswith("dist/print/")
    assert "final" in Path(pdf.path).name
    out = _root_join(doc_id, pdf.path)
    assert out.exists()
    assert out.read_bytes()[:4] == PDF_MAGIC


def test_web_profile_out_dir_outside_workspace_rejected(root: Path, doc_id: str) -> None:
    # sec.12 /: an out_dir escaping the workspace is rejected with the stable sandbox
    # message — BEFORE any Inkscape invocation, so no binary is needed for this test.
    with pytest.raises(ToolError) as exc:
        export_web_profile(doc_id, width_px=16, out_dir="../escape")
    assert "outside workspace" in str(exc.value)


def test_icon_set_out_dir_outside_workspace_rejected(root: Path, doc_id: str) -> None:
    with pytest.raises(ToolError) as exc:
        create_icon_set(doc_id, sizes=[16], out_dir="/etc")
    assert "outside workspace" in str(exc.value)


def test_print_profile_out_dir_outside_workspace_rejected(root: Path, doc_id: str) -> None:
    with pytest.raises(ToolError) as exc:
        export_print_profile(doc_id, out_dir="../../escape")
    assert "outside workspace" in str(exc.value)


def test_profile_out_dir_omitted_back_compat(doc_id: str) -> None:
    # Omitting out_dir keeps the managed per-doc exports dir (no behaviour change).
    # Validate via the engine path-resolution alone (no Inkscape needed): the engine's
    # _resolve_out_dir returns None when out_dir is omitted.
    from inkscape_mcp.config import get_settings
    from inkscape_mcp.render.cli import _resolve_out_dir

    entry = get_registry().get(doc_id)
    assert _resolve_out_dir(None, entry, get_settings()) is None


# ---: profile exports self-certify content truth ---------------------


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_print_profile_pdf_reports_vector_and_outlined(text_doc_id: str) -> None:
    # The print profile outlines text (--export-text-to-path) and embeds no raster, so the produced
    # PDF self-certifies as TRUE VECTOR: is_vector and fonts_outlined both True.
    result = export_print_profile(text_doc_id)
    pdf = result.artifacts[0]
    assert pdf.format == "pdf"
    assert pdf.is_vector is True
    assert pdf.fonts_outlined is True


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_web_profile_png_reports_opaque_pixels(doc_id: str) -> None:
    # The PNGs in the web set actually drew pixels, so each reports a non-zero opaque-pixel count
    # and is not flagged blank.
    result = export_web_profile(doc_id, width_px=64)
    pngs = [a for a in result.artifacts if a.format == "png"]
    assert pngs
    for png in pngs:
        assert png.opaque_px is not None
        assert png.opaque_px > 0
        assert png.all_blank is False
