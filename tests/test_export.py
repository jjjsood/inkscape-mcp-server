"""Export-tool tests: render_preview / export_document / export_object.

Inkscape-dependent tests are marked `@pytest.mark.inkscape` and actually run the local binary
(it IS available on this host). The registration test needs no Inkscape.
"""

from __future__ import annotations

import asyncio
import shutil
import struct
from pathlib import Path
from typing import Any

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult

from inkscape_mcp.config import ENV_MAX_EXPORT_PX, ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.server import mcp
from inkscape_mcp.tools.export import (
    ExportResult,
    FrameResult,
    PreviewResult,
    list_frames,
)
from inkscape_mcp.tools.export import capture_frame as _capture_frame_tool
from inkscape_mcp.tools.export import export_document as _export_document_tool
from inkscape_mcp.tools.export import export_object as _export_object_tool
from inkscape_mcp.tools.export import render_preview as _render_preview_tool


#: render/export now default to INLINE (returning a `ToolResult` that carries the structured
# payload in `structured_content` plus the PNG as an image content block). These shims unwrap that
# envelope back to the bare structured model so the existing structured-shape assertions below keep
# exercising the same fields; the inline/ToolResult behaviour itself is covered by the dedicated
# inline tests at the end of this module (which call the real tool functions).
def render_preview(*args: Any, **kwargs: Any) -> PreviewResult:
    r = _render_preview_tool(*args, **kwargs)
    return r if isinstance(r, PreviewResult) else PreviewResult(**r.structured_content)


def capture_frame(*args: Any, **kwargs: Any) -> FrameResult:
    r = _capture_frame_tool(*args, **kwargs)
    return r if isinstance(r, FrameResult) else FrameResult(**r.structured_content)


def export_document(*args: Any, **kwargs: Any) -> ExportResult:
    r = _export_document_tool(*args, **kwargs)
    return r if isinstance(r, ExportResult) else ExportResult(**r.structured_content)


def export_object(*args: Any, **kwargs: Any) -> ExportResult:
    r = _export_object_tool(*args, **kwargs)
    return r if isinstance(r, ExportResult) else ExportResult(**r.structured_content)


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
    """Join a returned path (`artifact_path` or `workspace_relative_path`) to the workspace ROOT.

    ONE LOCATION CONTRACT: both fields are root-relative and identical, so an agent opens
    any artifact by a single plain join to the workspace ROOT — no `find`/`stat` of the real file.
    """
    entry = get_registry().get(doc_id)
    return Path(entry.root) / ws_rel


def _png_dims(path: Path) -> tuple[int, int]:
    """Read the on-disk PNG (width, height) from its IHDR for dimension assertions."""
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    assert head[12:16] == b"IHDR"
    w, h = struct.unpack(">II", head[16:24])
    return int(w), int(h)


# --- registration (no Inkscape) ---------------------------------------------


def test_tools_registered_on_mcp(root: Path) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {
        "render_preview",
        "export_document",
        "export_object",
        "capture_frame",
        "list_frames",
    } <= names


# --- render_preview ----------------------------------------------------------


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_render_preview_produces_png(doc_id: str) -> None:
    result = render_preview(doc_id, width_px=200)
    assert result.format == "png"
    out = _root_join(doc_id, result.artifact_path)
    assert out.exists()
    assert out.read_bytes()[:4] == PNG_MAGIC
    assert result.width_px == 200
    # height follows the 100x80 aspect ratio
    assert result.height_px == 160
    # ONE LOCATION CONTRACT: artifact_path is root-relative (carries the doc base), not
    # absolute, and identical to workspace_relative_path.
    assert not result.artifact_path.startswith("/")
    assert result.artifact_path.startswith(f".inkscape-mcp/documents/{doc_id}/")
    assert "artifacts/preview/" in result.artifact_path
    assert result.artifact_path == result.workspace_relative_path


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_render_preview_workspace_relative_path_resolves(doc_id: str) -> None:
    #: the workspace_relative_path opens by a plain join to the workspace ROOT — no
    # find/stat — and carries the documented `.inkscape-mcp/documents/<doc_id>/...` base.
    result = render_preview(doc_id, width_px=120)
    assert not result.workspace_relative_path.startswith("/")
    assert result.workspace_relative_path.startswith(f".inkscape-mcp/documents/{doc_id}/")
    opened = _root_join(doc_id, result.workspace_relative_path)
    assert opened.exists()
    assert opened.read_bytes()[:4] == PNG_MAGIC


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_render_preview_non_clobbering(doc_id: str) -> None:
    #: two previews at the SAME width must NOT clobber — distinct, both still on disk.
    a = render_preview(doc_id, width_px=128)
    b = render_preview(doc_id, width_px=128)
    assert a.workspace_relative_path != b.workspace_relative_path
    assert _root_join(doc_id, a.workspace_relative_path).exists()
    assert _root_join(doc_id, b.workspace_relative_path).exists()


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_render_preview_name_tag_in_filename(doc_id: str) -> None:
    #: an optional caller name controls the output stem and is still non-clobbering.
    result = render_preview(doc_id, width_px=64, name="before")
    assert "before" in result.workspace_relative_path
    assert _root_join(doc_id, result.workspace_relative_path).exists()


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_render_preview_true_raster_dims(doc_id: str) -> None:
    #: reported dims equal the on-disk IHDR dims (non-square 1200-wide export).
    result = render_preview(doc_id, width_px=1200)
    out = _root_join(doc_id, result.workspace_relative_path)
    w, h = _png_dims(out)
    assert (result.width_px, result.height_px) == (w, h)
    assert result.width_px == 1200


# --- export_document ---------------------------------------------------------


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_document_png(doc_id: str) -> None:
    result = export_document(doc_id, "png", width_px=150)
    out = _root_join(doc_id, result.artifact_path)
    assert out.exists()
    assert out.read_bytes()[:4] == PNG_MAGIC
    # ONE LOCATION CONTRACT: artifact_path is the root-relative path (with the doc base).
    assert result.artifact_path.startswith(f".inkscape-mcp/documents/{doc_id}/")
    assert "artifacts/exports/" in result.artifact_path
    assert result.width_px == 150


# A non-square (1200x630, an OG-image aspect) document so a 1200-wide PNG export is exactly
# 1200x630 — used to assert reported dims == true on-disk raster dims.
SVG_OG = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
  <rect id="bg" x="0" y="0" width="1200" height="630" fill="#102030"/>
  <circle id="dot" cx="200" cy="200" r="120" fill="#ff8800"/>
</svg>
"""


@pytest.fixture
def og_doc_id(root: Path) -> str:
    src = root / "og.svg"
    src.write_bytes(SVG_OG)
    return get_registry().open_document(str(src)).doc_id


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_document_true_raster_dims_non_square(og_doc_id: str) -> None:
    # /: a 1200-wide export of a 1200x630 doc is 1200x630 on disk; reported dims must
    # equal the IHDR, NOT a page/viewBox-halved 600 (the S17 finding).
    result = export_document(og_doc_id, "png", width_px=1200)
    out = _root_join(og_doc_id, result.workspace_relative_path)
    w, h = _png_dims(out)
    assert (result.width_px, result.height_px) == (w, h)
    assert (w, h) == (1200, 630)


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_document_workspace_relative_path_resolves(doc_id: str) -> None:
    #: resolvable by a plain join to the workspace ROOT; carries the documented base.
    result = export_document(doc_id, "png", width_px=64)
    assert result.workspace_relative_path.startswith(f".inkscape-mcp/documents/{doc_id}/")
    assert _root_join(doc_id, result.workspace_relative_path).read_bytes()[:4] == PNG_MAGIC


# ---: ONE LOCATION CONTRACT (artifact_path == workspace_relative_path) ------------------


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_managed_export_unified_location_contract(doc_id: str) -> None:
    # A MANAGED export: artifact_path and workspace_relative_path are byte-identical, both
    # root-relative (carry the doc base), neither absolute, and BOTH open via a single join to the
    # workspace ROOT — no find/stat.
    result = export_document(doc_id, "png", width_px=48)
    assert result.artifact_path == result.workspace_relative_path
    assert not result.artifact_path.startswith("/")
    assert result.artifact_path.startswith(f".inkscape-mcp/documents/{doc_id}/")
    assert _root_join(doc_id, result.artifact_path).read_bytes()[:4] == PNG_MAGIC
    assert _root_join(doc_id, result.workspace_relative_path).read_bytes()[:4] == PNG_MAGIC


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_out_dir_export_unified_location_contract(root: Path, doc_id: str) -> None:
    # An OUT_DIR export: same contract. artifact_path == workspace_relative_path, both the
    # in-workspace relative path to the chosen dir, neither absolute, BOTH open via one root join.
    (root / "delivery").mkdir()
    result = export_document(doc_id, "png", width_px=48, out_dir="delivery", name_prefix="final")
    assert result.artifact_path == result.workspace_relative_path
    assert not result.artifact_path.startswith("/")
    assert result.artifact_path.startswith("delivery/")
    assert _root_join(doc_id, result.artifact_path).read_bytes()[:4] == PNG_MAGIC
    assert _root_join(doc_id, result.workspace_relative_path).read_bytes()[:4] == PNG_MAGIC


# --- out_dir --------------------------------------------------------


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_document_out_dir_in_workspace(root: Path, doc_id: str) -> None:
    # A caller-chosen, in-workspace out_dir is honored; the returned location resolves.
    (root / "out").mkdir()
    result = export_document(doc_id, "png", width_px=48, out_dir="out", name_prefix="hero")
    # Relative out_dir anchors to the workspace ROOT, so the file lands under <root>/out/.
    assert result.workspace_relative_path.startswith("out/")
    assert "hero" in result.workspace_relative_path
    opened = _root_join(doc_id, result.workspace_relative_path)
    assert opened.exists()
    assert opened.read_bytes()[:4] == PNG_MAGIC


def test_export_document_out_dir_outside_workspace_rejected(root: Path, doc_id: str) -> None:
    # sec.12: an out_dir escaping the workspace is rejected with the stable sandbox
    # message and NO host path — rejected before any Inkscape invocation.
    with pytest.raises(ToolError) as exc:
        export_document(doc_id, "png", width_px=48, out_dir="../escape")
    assert "outside workspace" in str(exc.value)
    assert str(root) not in str(exc.value)


def test_export_object_out_dir_outside_workspace_rejected(root: Path, doc_id: str) -> None:
    with pytest.raises(ToolError) as exc:
        export_object(doc_id, "dot", "png", width_px=24, out_dir="/etc")
    assert "outside workspace" in str(exc.value)


def test_out_dir_escape_creates_no_directory_outside_workspace(root: Path, doc_id: str) -> None:
    # sec.12: a `../`-escaping out_dir must be rejected BEFORE any directory is created, so no
    # side-effect dir is planted outside the workspace.
    escape_target = root.parent / "escape_sidecar"
    assert not escape_target.exists()
    with pytest.raises(ToolError):
        export_document(doc_id, "png", width_px=16, out_dir="../escape_sidecar")
    assert not escape_target.exists()


def test_out_dir_symlinked_component_escape_rejected(root: Path, doc_id: str) -> None:
    # sec.12 (class, HIGH): a pre-existing symlink ON the out_dir path that points OUTSIDE
    # the workspace is rejected BEFORE any directory is created, and nothing lands at the link
    # target — closing the symlink-redirect TOCTOU on caller-chosen export dirs.
    outside = root.parent / "sym_target"
    outside.mkdir()
    (root / "evil").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ToolError) as exc:
        export_document(doc_id, "png", width_px=16, out_dir="evil/sub")
    assert "outside workspace" in str(exc.value)
    assert str(root) not in str(exc.value)
    assert not (outside / "sub").exists()


def test_out_dir_dotdot_component_rejected(root: Path, doc_id: str) -> None:
    # A literal `..` component in out_dir is refused outright (no lexical-collapse ambiguity).
    with pytest.raises(ToolError) as exc:
        export_document(doc_id, "png", width_px=16, out_dir="a/../b")
    assert "outside workspace" in str(exc.value)


def test_resolve_out_dir_creates_nested_dirs_safely(root: Path, doc_id: str) -> None:
    # The TOCTOU-safe O_NOFOLLOW descent still creates a legitimate nested in-workspace out_dir.
    from inkscape_mcp.render.cli import _resolve_out_dir

    entry = get_registry().get(doc_id)
    resolved = _resolve_out_dir("deep/nested/exports", entry, get_settings())
    assert resolved is not None
    assert resolved.is_dir()
    assert (root / "deep" / "nested" / "exports").resolve() == resolved.resolve()


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_document_default_dir_back_compat(doc_id: str) -> None:
    # Omitting out_dir keeps the managed per-doc exports dir (back-compat).
    result = export_document(doc_id, "png", width_px=48)
    assert "artifacts/exports/" in result.artifact_path


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_document_pdf_has_pdf_magic(doc_id: str) -> None:
    result = export_document(doc_id, "pdf")
    out = _root_join(doc_id, result.artifact_path)
    assert out.exists()
    assert out.read_bytes()[:4] == PDF_MAGIC
    assert result.format == "pdf"
    # vector output: no raster dimensions
    assert result.width_px is None
    assert result.height_px is None


# ---: export self-certifies content truth ----------------------------


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_document_png_reports_opaque_pixels(doc_id: str) -> None:
    # A real PNG export drew pixels, so it reports a non-zero opaque-pixel count and is not blank
    # . The vector-only fields stay None for a raster.
    result = export_document(doc_id, "png", width_px=64)
    assert result.opaque_px is not None
    assert result.opaque_px > 0
    assert result.all_blank is False
    assert result.is_vector is None
    assert result.fonts_outlined is None


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_document_pdf_reports_vector_truth(doc_id: str) -> None:
    # A plain PDF export of a shapes-only document embeds no raster image, so it self-certifies
    # is_vector=True; the raster fields stay None.
    result = export_document(doc_id, "pdf")
    assert result.is_vector is True
    assert result.fonts_outlined is not None
    assert result.opaque_px is None
    assert result.all_blank is None


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_render_preview_reports_opaque_pixels(doc_id: str) -> None:
    # A real preview render drew pixels.
    result = render_preview(doc_id, width_px=64)
    assert result.opaque_px is not None
    assert result.opaque_px > 0
    assert result.all_blank is False


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_render_preview_blank_document_reports_all_blank(root: Path) -> None:
    # A transparent (empty) canvas renders to a fully transparent PNG, so the preview self-reports
    # all_blank=True / opaque_px=0 — the "render actually drew something" check.
    blank = root / "blank.svg"
    blank.write_bytes(
        b'<?xml version="1.0"?>\n'
        b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" '
        b'viewBox="0 0 40 40"></svg>\n'
    )
    blank_id = get_registry().open_document(str(blank)).doc_id
    result = render_preview(blank_id, width_px=40)
    assert result.opaque_px == 0
    assert result.all_blank is True


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_document_svg(doc_id: str) -> None:
    result = export_document(doc_id, "svg")
    out = _root_join(doc_id, result.artifact_path)
    assert out.exists()
    head = out.read_bytes()[:64].lstrip()
    assert head.startswith(b"<?xml") or head.startswith(b"<svg")
    assert result.format == "svg"


def test_export_document_rejects_unknown_format(doc_id: str) -> None:
    with pytest.raises(ToolError) as exc:
        export_document(doc_id, "gif")
    assert "unsupported export format" in str(exc.value)
    #: a capability-absent (unsupported format) error names the discovery tool.
    assert "list_capabilities" in str(exc.value)


# --- capability-absent errors name discovery / alternative tools ------


def test_map_failure_missing_engine_names_list_capabilities() -> None:
    #: when the Inkscape engine cannot be launched (ProcessError; e.g. no binary on this
    # runtime) the client-facing error NAMES list_capabilities and carries no host path (sec.12).
    from inkscape_mcp.tools.export import _map_failure
    from inkscape_mcp.workspace.subprocess_exec import ProcessError

    err = _map_failure(ProcessError("inkscape binary not found"))
    msg = str(err)
    assert "list_capabilities" in msg
    assert "unavailable" in msg
    # Host-path-free: the raw engine detail is not leaked verbatim.
    assert "binary not found" not in msg


# --- export_object -----------------------------------------------------------


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_object_valid_id(doc_id: str) -> None:
    result = export_object(doc_id, "dot", "png", width_px=80)
    out = _root_join(doc_id, result.artifact_path)
    assert out.exists()
    assert out.read_bytes()[:4] == PNG_MAGIC
    assert "obj-dot" in result.artifact_path


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_object_true_clipped_dims(doc_id: str) -> None:
    #: the "dot" circle r=20 has a 40x40 bbox; clipped + scaled to width 36 yields a 36x36
    # raster. Reported dims must equal the on-disk IHDR (NOT the 100x80 document/page size).
    result = export_object(doc_id, "dot", "png", width_px=36)
    out = _root_join(doc_id, result.workspace_relative_path)
    w, h = _png_dims(out)
    assert (result.width_px, result.height_px) == (w, h)
    assert (w, h) == (36, 36)


def test_export_object_unknown_id_raises(doc_id: str) -> None:
    with pytest.raises(ToolError) as exc:
        export_object(doc_id, "no_such_id", "png")
    assert "not found" in str(exc.value)


@pytest.mark.parametrize("bad_id", ["a b", "../etc/passwd", "id;rm -rf", "a/b", "$(x)"])
def test_export_object_malicious_id_rejected(doc_id: str, bad_id: str) -> None:
    # A malicious / unsafe id must be rejected by the safe-charset gate BEFORE it could ever
    # reach argv. None of these match the safe SVG-id charset, so no Inkscape call happens.
    with pytest.raises(ToolError) as exc:
        export_object(doc_id, bad_id, "png")
    assert "not valid" in str(exc.value)


# --- limits ------------------------------------------------------------------


def test_oversized_preview_rejected_before_invoking(
    doc_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shrink the pixel cap to 2 px and request a 500 px preview: must be rejected by the
    # dimension pre-check before Inkscape is ever invoked.
    monkeypatch.setenv(ENV_MAX_EXPORT_PX, "2")
    get_settings.cache_clear()
    with pytest.raises(ToolError) as exc:
        render_preview(doc_id, width_px=500)
    assert "limit" in str(exc.value)


def test_oversized_export_document_rejected(doc_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_MAX_EXPORT_PX, "2")
    get_settings.cache_clear()
    with pytest.raises(ToolError):
        export_document(doc_id, "png", width_px=500)


# --- unknown doc -------------------------------------------------------------


def test_unknown_doc_id_render(root: Path) -> None:
    with pytest.raises(ToolError) as exc:
        render_preview("d_missing", width_px=50)
    assert "not found" in str(exc.value)


def test_unknown_doc_id_export(root: Path) -> None:
    with pytest.raises(ToolError) as exc:
        export_document("d_missing", "png")
    assert "not found" in str(exc.value)


# --- capture_frame / list_frames (numbered screenshot series) ----------------


def test_next_frame_index_filesystem_derived(tmp_path: Path) -> None:
    # Binary-free: the next index is max(existing frame-NNN) + 1, ignoring non-frame files. This is
    # what makes the counter stateless / restart-proof (no in-memory series state).
    from inkscape_mcp.render.cli import _next_frame_index

    assert _next_frame_index(tmp_path) == 1
    (tmp_path / "frame-001.png").touch()
    (tmp_path / "frame-007.png").touch()
    (tmp_path / "frame-003-step.png").touch()
    (tmp_path / "notes.txt").touch()
    (tmp_path / "preview-foo.png").touch()
    assert _next_frame_index(tmp_path) == 8


def test_list_frames_empty_for_unused_series(doc_id: str) -> None:
    # No render needed: an unused series yields an empty listing with the resolved series name.
    result = list_frames(doc_id, series="never_used")
    assert result.series == "never_used"
    assert result.frames == []


def test_list_frames_unknown_doc(root: Path) -> None:
    with pytest.raises(ToolError) as exc:
        list_frames("d_missing")
    assert "not found" in str(exc.value)


def test_capture_frame_unknown_doc(root: Path) -> None:
    with pytest.raises(ToolError) as exc:
        capture_frame("d_missing")
    assert "not found" in str(exc.value)


def test_capture_frame_negative_width_rejected(doc_id: str) -> None:
    # A non-positive width is refused before it can reach `--export-width=` (no Inkscape call).
    with pytest.raises(ToolError):
        capture_frame(doc_id, width_px=-5)


def test_list_frames_skips_symlinks(root: Path, doc_id: str) -> None:
    # sec.12: a symlink planted in the managed frames dir whose name matches the frame stem must NOT
    # be returned as a resolvable workspace path (following it could escape the sandbox).
    from inkscape_mcp.workspace import sandbox

    frames_dir = sandbox.artifacts_dir(root, doc_id) / "frames" / "run"
    frames_dir.mkdir(parents=True, exist_ok=True)
    (frames_dir / "frame-001.png").write_bytes(PNG_MAGIC)
    outside = root.parent / "secret.png"
    outside.write_bytes(b"secret")
    (frames_dir / "frame-002.png").symlink_to(outside)

    listing = list_frames(doc_id)
    assert [f.frame_index for f in listing.frames] == [1]


def test_capture_frame_oversized_rejected(doc_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Same pixel-cap pre-check as render_preview: rejected before Inkscape is invoked.
    monkeypatch.setenv(ENV_MAX_EXPORT_PX, "2")
    get_settings.cache_clear()
    with pytest.raises(ToolError) as exc:
        capture_frame(doc_id, width_px=500)
    assert "limit" in str(exc.value)


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_capture_frame_numbered_series(doc_id: str) -> None:
    # Successive captures build frame-001, frame-002 … with a monotonic 1-based index, default
    # series "run", both PNGs on disk under artifacts/frames/run/.
    a = capture_frame(doc_id)
    b = capture_frame(doc_id)
    assert (a.series, a.frame_index) == ("run", 1)
    assert (b.series, b.frame_index) == ("run", 2)
    assert a.artifact_path.startswith(f".inkscape-mcp/documents/{doc_id}/")
    assert "artifacts/frames/run/" in a.artifact_path
    assert a.artifact_path.endswith("frame-001.png")
    assert b.artifact_path.endswith("frame-002.png")
    assert a.artifact_path == a.workspace_relative_path
    assert _root_join(doc_id, a.artifact_path).read_bytes()[:4] == PNG_MAGIC
    assert _root_join(doc_id, b.artifact_path).read_bytes()[:4] == PNG_MAGIC


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_capture_frame_index_survives_external_frames(doc_id: str) -> None:
    # Filesystem-derived counter: seeding higher-numbered frames (as a restarted server would find
    # on disk) makes the next capture continue past them rather than clobber.
    first = capture_frame(doc_id, series="demo")
    series_dir = _root_join(doc_id, first.artifact_path).parent
    (series_dir / "frame-002.png").touch()
    (series_dir / "frame-005.png").touch()
    nxt = capture_frame(doc_id, series="demo")
    assert nxt.frame_index == 6
    assert nxt.artifact_path.endswith("frame-006.png")


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_capture_frame_distinct_series_independent_counters(doc_id: str) -> None:
    a = capture_frame(doc_id, series="alpha")
    b = capture_frame(doc_id, series="beta")
    assert (a.series, a.frame_index) == ("alpha", 1)
    assert (b.series, b.frame_index) == ("beta", 1)
    assert "artifacts/frames/alpha/" in a.artifact_path
    assert "artifacts/frames/beta/" in b.artifact_path


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_capture_frame_label_folded_into_name(doc_id: str) -> None:
    result = capture_frame(doc_id, label="after-recolor")
    assert result.frame_index == 1
    assert result.artifact_path.endswith("frame-001-after-recolor.png")
    assert _root_join(doc_id, result.artifact_path).read_bytes()[:4] == PNG_MAGIC


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_capture_frame_series_sanitized_no_escape(root: Path, doc_id: str) -> None:
    # sec.12: a path-traversal series name is sanitized to a single in-`frames` folder; nothing is
    # created outside the workspace.
    escape_target = root.parent / "escape"
    result = capture_frame(doc_id, series="../escape")
    assert result.series == "escape"
    assert "artifacts/frames/escape/" in result.artifact_path
    assert not (escape_target / "frame-001.png").exists()


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_capture_frame_true_raster_dims(doc_id: str) -> None:
    # Reported dims equal the on-disk IHDR (same render path as render_preview).
    result = capture_frame(doc_id, width_px=200)
    w, h = _png_dims(_root_join(doc_id, result.workspace_relative_path))
    assert (result.width_px, result.height_px) == (w, h)
    assert (result.width_px, result.height_px) == (200, 160)


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_list_frames_orders_by_index(doc_id: str) -> None:
    for _ in range(3):
        capture_frame(doc_id, series="run3")
    listing = list_frames(doc_id, series="run3")
    assert listing.series == "run3"
    assert [f.frame_index for f in listing.frames] == [1, 2, 3]
    for frame in listing.frames:
        opened = _root_join(doc_id, frame.workspace_relative_path)
        assert opened.read_bytes()[:4] == PNG_MAGIC
        assert frame.artifact_path == frame.workspace_relative_path


# --- inline raster ----------------------------------------------------


def _image_blocks(result: ToolResult) -> list[object]:
    """The image content blocks carried by a `ToolResult` (MCP `ImageContent`)."""
    return [c for c in result.content if getattr(c, "type", None) == "image"]


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_render_preview_inline_default_returns_image(doc_id: str) -> None:
    #: default inline=True returns a ToolResult carrying the structured payload AND the PNG
    # as an MCP image content block, so the agent sees the render without a second Read.
    result = _render_preview_tool(doc_id, width_px=120)
    assert isinstance(result, ToolResult)
    assert len(_image_blocks(result)) == 1
    # structured payload is preserved and machine-readable.
    assert result.structured_content is not None
    assert result.structured_content["format"] == "png"
    assert result.structured_content["width_px"] == 120
    assert result.structured_content["artifact_path"].startswith(
        f".inkscape-mcp/documents/{doc_id}/"
    )


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_render_preview_inline_false_returns_bare_model(doc_id: str) -> None:
    # opt-out: inline=False returns the bare structured model, no image content block.
    result = _render_preview_tool(doc_id, width_px=120, inline=False)
    assert isinstance(result, PreviewResult)
    assert result.format == "png"


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_render_preview_inline_over_threshold_skips_embed(doc_id: str) -> None:
    # gate: an artifact larger than max_output_bytes is NOT embedded (file still produced),
    # so the bare structured model is returned.
    result = _render_preview_tool(doc_id, width_px=120, max_output_bytes=1)
    assert isinstance(result, PreviewResult)
    assert _root_join(doc_id, result.workspace_relative_path).read_bytes()[:4] == PNG_MAGIC


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_document_png_inline_default_returns_image(doc_id: str) -> None:
    result = _export_document_tool(doc_id, "png", width_px=96)
    assert isinstance(result, ToolResult)
    assert len(_image_blocks(result)) == 1
    assert result.structured_content is not None
    assert result.structured_content["format"] == "png"


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_document_pdf_never_inlined(doc_id: str) -> None:
    #: vector outputs (PDF/SVG) are never embedded as an image content block — bare model.
    result = _export_document_tool(doc_id, "pdf")
    assert isinstance(result, ExportResult)
    assert result.format == "pdf"
    assert _root_join(doc_id, result.workspace_relative_path).read_bytes()[:4] == PDF_MAGIC


# --- real staleness tracking -----------------------------------------


def test_compute_stale_pure_helper(tmp_path: Path) -> None:
    # Pure, binary-free: an artifact OLDER than its working copy is stale; a NEWER one is not, and a
    # missing path resolves to "not stale / unknown" (False) rather than raising.
    import os

    from inkscape_mcp.render.cli import compute_stale

    working = tmp_path / "working.svg"
    artifact = tmp_path / "out.png"
    working.write_bytes(b"<svg/>")
    artifact.write_bytes(PNG_MAGIC)

    # Artifact produced AFTER the working copy => not stale.
    os.utime(working, (1000, 1000))
    os.utime(artifact, (2000, 2000))
    assert compute_stale(working, artifact) is False

    # Working copy edited AFTER the artifact => stale.
    os.utime(working, (3000, 3000))
    assert compute_stale(working, artifact) is True

    # A missing artifact => unknown (False), never an error.
    assert compute_stale(working, tmp_path / "gone.png") is False


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_fresh_artifact_not_stale(doc_id: str) -> None:
    #: a freshly produced artifact reflects the current working copy => stale is False.
    result = export_document(doc_id, "png", width_px=64)
    assert result.stale is False


@pytest.mark.inkscape
@pytest.mark.skipif(not inkscape_available, reason="inkscape not on PATH")
def test_export_result_recompute_stale_flips_after_edit(doc_id: str) -> None:
    #: an artifact produced BEFORE a later working-copy edit reports stale=True once the
    # working copy's mtime is advanced past the artifact's. We drive the real engine function (so
    # the produce-time source paths are captured) and then simulate a later edit via os.utime.
    import os

    from inkscape_mcp.render.cli import export_document as _engine_export

    result = _engine_export(doc_id, "png", width_px=64)
    assert result.stale is False

    artifact_abs = _root_join(doc_id, result.workspace_relative_path)
    working = Path(get_registry().get(doc_id).working_path)
    artifact_mtime = artifact_abs.stat().st_mtime
    # Simulate the working copy being edited AFTER the artifact was produced.
    os.utime(working, (artifact_mtime + 10, artifact_mtime + 10))

    assert result.recompute_stale() is True
    assert result.stale is True
