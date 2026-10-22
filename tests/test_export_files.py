"""Single-file exports: `export_document` / `export_object` (engine) and `export` file targets.

`FakeInkscape` stands in for the `inkscape` binary at the `run_inkscape` seam: it parses the real
argv the engine builds and writes a matching stub artifact (true PNG dims, `%PDF-<version>`, plain
SVG). So the engine's own logic (paths, naming, sandbox, caps, location contract, staleness) runs
for real and hermetically. Tests that need the real renderer carry `@pytest.mark.inkscape`.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_MAX_EXPORT_PX, ENV_MAX_OUTPUT_BYTES, get_settings
from inkscape_mcp.registry import get_registry
from inkscape_mcp.render import cli
from inkscape_mcp.render.cli import (
    InvalidObjectId,
    RenderError,
    compute_stale,
    export_document,
    export_object,
)
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.limits import LimitExceeded
from inkscape_mcp.workspace.paths import SandboxViolation
from inkscape_mcp.workspace.subprocess_exec import ProcessError, ProcessResult
from tests.tool_support import call, open_doc

#: 100x80 page: a 100 px wide export is 100x80 (aspect 0.8); `dot` is a 40x40 circle.
ART = (
    b'<?xml version="1.0"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="80" viewBox="0 0 100 80">'
    b'<rect id="bg" x="0" y="0" width="100" height="80" fill="#ffffff"/>'
    b'<circle id="dot" cx="30" cy="30" r="20" fill="#ff0000"/>'
    b'<rect id="box" x="55" y="10" width="30" height="40" fill="#0000ff"/>'
    b"</svg>"
)
PNG_MAGIC = b"\x89PNG"


class FakeInkscape:
    """A `run_inkscape` replacement: records argv, writes the artifact the argv asks for."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.blank = False  # write a fully transparent PNG
        self.svg_body = b""  # extra bytes appended to SVG output (to blow a budget)
        self.error: Exception | None = None

    def __call__(self, args: list[str], settings: object = None) -> ProcessResult:
        self.calls.append(list(args))
        if self.error is not None:
            raise self.error
        opts = dict(a[2:].split("=", 1) for a in args if a.startswith("--") and "=" in a)
        out = Path(opts["export-filename"])
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix == ".png":
            width = int(opts.get("export-width", 100))
            height = width if "export-id" in opts else round(width * 0.8)
            self._png(out, width, height)
        elif out.suffix == ".pdf":
            out.write_bytes(f"%PDF-{opts.get('export-pdf-version', '1.5')}\n%%EOF".encode())
        else:
            out.write_bytes(
                b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>' + self.svg_body
            )
        return ProcessResult(
            args=args, returncode=0, stdout="", stderr="", duration_s=0.0, timed_out=False
        )

    def _png(self, out: Path, width: int, height: int) -> None:
        from PIL import Image

        im = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        if not self.blank:
            im.paste((255, 0, 0, 255), (0, 0, max(1, width // 2), max(1, height // 2)))
        im.save(out)


def install_fake(monkeypatch: pytest.MonkeyPatch) -> FakeInkscape:
    """Route the engine's `run_inkscape` to a fresh `FakeInkscape` (shared by the export tests)."""
    fake = FakeInkscape()
    monkeypatch.setattr(cli, "run_inkscape", fake)
    return fake


@pytest.fixture
def fake_inkscape(monkeypatch: pytest.MonkeyPatch, tool_ws: Path) -> FakeInkscape:
    return install_fake(monkeypatch)


@pytest.fixture
def doc(tool_ws: Path) -> str:
    return open_doc(tool_ws, "art.svg", ART)


def _file(doc_id: str, rel: str) -> Path:
    return Path(get_registry().get(doc_id).root) / rel


def _png_dims(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[12:16] == b"IHDR"
    w, h = struct.unpack(">II", head[16:24])
    return int(w), int(h)


def _export(app, doc_id, targets, **kw):
    return call(app, "export", {"doc_id": doc_id, "targets": targets, **kw}).data


# --- engine: results, naming, location contract -----------------------------------------------


def test_png_reports_true_dims_and_one_location(fake_inkscape, doc) -> None:
    result = export_document(doc, "png", width_px=150)
    out = _file(doc, result.workspace_relative_path)
    assert out.read_bytes()[:4] == PNG_MAGIC
    assert (result.width_px, result.height_px) == _png_dims(out) == (150, 120)
    # one location contract: both fields are the same root-relative path with the doc base
    assert result.artifact_path == result.workspace_relative_path
    assert not result.artifact_path.startswith("/")
    assert result.artifact_path.startswith(f".inkscape-mcp/documents/{doc}/artifacts/exports/")
    assert result.opaque_px and result.opaque_px > 0 and result.all_blank is False
    assert result.is_vector is None and result.stale is False


def test_pdf_and_svg_are_vector_without_raster_dims(fake_inkscape, doc) -> None:
    pdf = export_document(doc, "pdf")
    assert _file(doc, pdf.artifact_path).read_bytes()[:4] == b"%PDF"
    assert (pdf.width_px, pdf.height_px, pdf.opaque_px) == (None, None, None)
    assert pdf.is_vector is True and pdf.fonts_outlined is True
    svg = export_document(doc, "svg", width_px=999)  # width is ignored for vector output
    assert svg.width_px is None
    assert "--export-width=999" not in fake_inkscape.calls[-1]
    assert "--export-plain-svg" in fake_inkscape.calls[-1]


def test_blank_render_reports_all_blank(fake_inkscape, doc) -> None:
    fake_inkscape.blank = True
    result = export_document(doc, "png", width_px=40)
    assert result.opaque_px == 0 and result.all_blank is True


def test_object_export_clips_to_the_object(fake_inkscape, doc) -> None:
    result = export_object(doc, "dot", "png", width_px=36)
    argv = fake_inkscape.calls[-1]
    assert "--export-id=dot" in argv and "--export-id-only" in argv
    assert "--export-area-page" not in argv
    assert "obj-dot" in result.artifact_path
    assert (result.width_px, result.height_px) == (36, 36)


@pytest.mark.parametrize("bad_id", ["a b", "../etc/passwd", "id;rm -rf", "a/b", "$(x)", ""])
def test_unsafe_object_id_never_reaches_argv(fake_inkscape, doc, bad_id) -> None:
    with pytest.raises(InvalidObjectId):
        export_object(doc, bad_id, "png")
    assert fake_inkscape.calls == []


@pytest.mark.parametrize("fn", [export_document, export_object])
def test_unsupported_format_is_refused(fake_inkscape, doc, fn) -> None:
    args = ("dot", "gif") if fn is export_object else ("gif",)
    with pytest.raises(RenderError, match="unsupported export format"):
        fn(doc, *args)
    assert fake_inkscape.calls == []


@pytest.mark.parametrize("flag", ["--shell", "--export-filename=/tmp/x", "--export-filename"])
def test_internal_extra_args_must_be_export_flags(fake_inkscape, doc, flag) -> None:
    with pytest.raises(RenderError, match="--export-"):
        export_document(doc, "pdf", _extra_args=[flag])
    assert fake_inkscape.calls == []


def test_name_prefix_is_sanitised_into_the_filename(fake_inkscape, doc, tool_ws) -> None:
    result = export_document(doc, "png", width_px=16, name_prefix="a/../b c")
    name = Path(result.artifact_path).name
    assert "a-..-b-c" in name and "/" not in name
    assert Path(result.artifact_path).parent.as_posix().endswith("artifacts/exports")


# --- engine: out_dir sandbox ---------------------------------------------------------------


def test_out_dir_in_workspace_is_honoured(fake_inkscape, doc, tool_ws) -> None:
    result = export_document(doc, "png", width_px=48, out_dir="deep/nested/out", name_prefix="hero")
    assert result.artifact_path == result.workspace_relative_path
    assert result.artifact_path.startswith("deep/nested/out/") and "hero" in result.artifact_path
    assert _file(doc, result.artifact_path).read_bytes()[:4] == PNG_MAGIC
    assert (tool_ws / "deep" / "nested" / "out").is_dir()


@pytest.mark.parametrize("out_dir", ["../escape", "/etc", "a/../b", "../../x/y"])
def test_out_dir_escape_is_rejected_without_side_effects(
    fake_inkscape, doc, tool_ws, out_dir
) -> None:
    before = set(tool_ws.parent.iterdir())
    with pytest.raises(SandboxViolation, match="outside workspace") as exc:
        export_document(doc, "png", width_px=16, out_dir=out_dir)
    assert str(tool_ws) not in str(exc.value)
    assert set(tool_ws.parent.iterdir()) == before and not (tool_ws / "b").exists()
    assert fake_inkscape.calls == []


def test_out_dir_through_a_symlink_out_of_the_workspace_is_rejected(
    fake_inkscape, doc, tool_ws
) -> None:
    outside = tool_ws.parent / "sym_target"
    outside.mkdir()
    (tool_ws / "evil").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SandboxViolation, match="outside workspace") as exc:
        export_object(doc, "dot", "png", width_px=16, out_dir="evil/sub")
    assert str(tool_ws) not in str(exc.value)
    assert not (outside / "sub").exists() and fake_inkscape.calls == []


def test_resolve_out_dir_defaults_to_none_and_creates_nested_dirs(doc, tool_ws) -> None:
    entry = get_registry().get(doc)
    assert cli._resolve_out_dir(None, entry, get_settings()) is None
    resolved = cli._resolve_out_dir("x/y/z", entry, get_settings())
    assert resolved is not None and resolved.resolve() == (tool_ws / "x" / "y" / "z").resolve()
    assert resolved.is_dir()


# --- engine: limits ------------------------------------------------------------------------


@pytest.mark.parametrize("fn", ["document", "object"])
def test_oversized_png_is_rejected_before_inkscape(fake_inkscape, doc, monkeypatch, fn) -> None:
    monkeypatch.setenv(ENV_MAX_EXPORT_PX, "2")
    get_settings.cache_clear()
    with pytest.raises(LimitExceeded):
        if fn == "document":
            export_document(doc, "png", width_px=500)
        else:
            export_object(doc, "dot", "png", width_px=500)
    assert fake_inkscape.calls == []


def test_oversized_output_is_deleted_and_refused(fake_inkscape, doc, tool_ws, monkeypatch) -> None:
    monkeypatch.setenv(ENV_MAX_OUTPUT_BYTES, "20")
    get_settings.cache_clear()
    with pytest.raises(LimitExceeded):
        export_document(doc, "svg")
    exports = sandbox.exports_dir(tool_ws, doc)
    assert not any(exports.iterdir())


def test_engine_failure_leaves_no_artifact(fake_inkscape, doc) -> None:
    fake_inkscape.error = ProcessError("inkscape binary not found")
    with pytest.raises(ProcessError):
        export_document(doc, "png")


def test_unknown_document_is_a_keyerror(fake_inkscape, tool_ws) -> None:
    with pytest.raises(KeyError):
        export_document("d_missing", "png")


# --- engine: staleness ----------------------------------------------------------------------


def test_compute_stale_compares_mtimes(tmp_path: Path) -> None:
    working, artifact = tmp_path / "working.svg", tmp_path / "out.png"
    working.write_bytes(b"<svg/>")
    artifact.write_bytes(PNG_MAGIC)
    os.utime(working, (1000, 1000))
    os.utime(artifact, (2000, 2000))
    assert compute_stale(working, artifact) is False
    os.utime(working, (3000, 3000))
    assert compute_stale(working, artifact) is True
    assert compute_stale(working, tmp_path / "gone.png") is False  # unknown is never stale


def test_result_goes_stale_after_the_working_copy_changes(fake_inkscape, doc) -> None:
    result = export_document(doc, "png", width_px=64)
    assert result.stale is False
    working = Path(get_registry().get(doc).working_path)
    later = Path(result.artifact_abspath or "").stat().st_mtime + 10
    os.utime(working, (later, later))
    assert result.recompute_stale() is True and result.stale is True
    # the produce-time host paths never reach a client
    assert (
        "working_path" not in result.model_dump() and "artifact_abspath" not in result.model_dump()
    )


# --- tool: file targets -----------------------------------------------------------------------


def test_tool_artifacts_resolve_by_one_root_join(tool_app, fake_inkscape, doc, tool_ws) -> None:
    data = _export(
        tool_app,
        doc,
        [
            {"kind": "file", "format": "png", "width_px": 64},
            {"kind": "file", "format": "pdf"},
            {"kind": "file", "format": "png", "object_id": "dot", "width_px": 24},
        ],
    )
    assert [a["format"] for a in data["artifacts"]] == ["png", "pdf", "png"]
    for art in data["artifacts"]:
        assert art["path"].startswith(f".inkscape-mcp/documents/{doc}/artifacts/exports/")
        assert _file(doc, art["path"]).is_file() and art["bytes"] > 0
    assert data["artifacts"][0]["width_px"] == 64
    assert data["artifacts"][2]["object_id"] == "dot"
    assert "width_px" not in data["artifacts"][1]  # vector output has no raster dims


def test_tool_out_dir_and_prefix(tool_app, fake_inkscape, doc, tool_ws) -> None:
    data = _export(
        tool_app,
        doc,
        [{"kind": "file", "format": "svg"}],
        out_dir="delivery",
        name_prefix="final",
    )
    path = data["artifacts"][0]["path"]
    assert path.startswith("delivery/") and "final" in path and (tool_ws / path).is_file()


@pytest.mark.parametrize("out_dir", ["/etc", "a/../b", "../escape"])
def test_tool_out_dir_escape_is_rejected(tool_app, fake_inkscape, doc, out_dir) -> None:
    with pytest.raises(ToolError, match=r"targets\[0\] \(file\): path rejected"):
        _export(tool_app, doc, [{"kind": "file", "format": "png"}], out_dir=out_dir)
    assert fake_inkscape.calls == []


@pytest.mark.parametrize("bad_id", ["a b", "../etc/passwd", "id;rm -rf", "a/b", "$(x)"])
def test_tool_rejects_unsafe_object_ids(tool_app, fake_inkscape, doc, bad_id) -> None:
    with pytest.raises(ToolError, match="object id is not valid"):
        _export(tool_app, doc, [{"kind": "file", "format": "png", "object_id": bad_id}])
    assert fake_inkscape.calls == []


def test_tool_width_over_the_pixel_cap_is_refused(
    tool_app, fake_inkscape, doc, monkeypatch
) -> None:
    monkeypatch.setenv(ENV_MAX_EXPORT_PX, "2")
    get_settings.cache_clear()
    with pytest.raises(ToolError, match="pixel cap"):
        _export(tool_app, doc, [{"kind": "file", "format": "png", "width_px": 500}])
    assert fake_inkscape.calls == []


def test_tool_limit_error_uses_the_export_wording(
    tool_app, fake_inkscape, doc, monkeypatch
) -> None:
    monkeypatch.setenv(ENV_MAX_OUTPUT_BYTES, "20")
    get_settings.cache_clear()
    with pytest.raises(ToolError, match="export exceeds the configured size or dimension limit"):
        _export(tool_app, doc, [{"kind": "file", "format": "svg"}])


def test_tool_missing_engine_is_actionable_and_leaks_nothing(tool_app, fake_inkscape, doc) -> None:
    fake_inkscape.error = ProcessError("inkscape binary not found")
    with pytest.raises(ToolError) as exc:
        _export(tool_app, doc, [{"kind": "file", "format": "png"}])
    msg = str(exc.value)
    assert "help(topic='diagnose')" in msg and "binary not found" not in msg


def test_tool_unknown_document(tool_app, tool_ws) -> None:
    with pytest.raises(ToolError, match="document id not found"):
        _export(tool_app, "d_missing", [{"kind": "file", "format": "png"}])


# --- real renderer -------------------------------------------------------------------------


@pytest.mark.inkscape
def test_real_png_dims_match_ihdr_for_a_non_square_page(tool_ws) -> None:
    og = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" '
        b'viewBox="0 0 1200 630"><rect width="1200" height="630" fill="#102030"/></svg>'
    )
    doc_id = open_doc(tool_ws, "og.svg", og)
    result = export_document(doc_id, "png", width_px=1200)
    w, h = _png_dims(_file(doc_id, result.workspace_relative_path))
    assert (result.width_px, result.height_px) == (w, h) == (1200, 630)


@pytest.mark.inkscape
def test_real_object_export_is_clipped_to_the_object(doc) -> None:
    result = export_object(doc, "dot", "png", width_px=36)
    w, h = _png_dims(_file(doc, result.workspace_relative_path))
    assert (result.width_px, result.height_px) == (w, h) == (36, 36)


@pytest.mark.inkscape
def test_real_pdf_and_svg(doc) -> None:
    pdf = export_document(doc, "pdf")
    assert _file(doc, pdf.artifact_path).read_bytes()[:4] == b"%PDF"
    assert pdf.is_vector is True and pdf.width_px is None
    svg = export_document(doc, "svg")
    assert _file(doc, svg.artifact_path).read_bytes()[:64].lstrip().startswith((b"<?xml", b"<svg"))


@pytest.mark.inkscape
def test_real_blank_document_reports_all_blank(tool_ws) -> None:
    blank = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 40 40"/>'
    doc_id = open_doc(tool_ws, "blank.svg", blank)
    result = export_document(doc_id, "png", width_px=40)
    assert result.opaque_px == 0 and result.all_blank is True
