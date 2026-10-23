"""Render engine and inline images: `render.cli` previews / frames, `render.inline`, `render` tool.

Runs the real engine (paths, naming, caps, frame numbering, sandbox, staleness) against
`FakeInkscape` (see `test_export_files`), checks the inline PNG the tool attaches and the tool's
argument handling. Tests that need the real renderer carry `@pytest.mark.inkscape`.
"""

from __future__ import annotations

import base64
import io
import struct
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from PIL import Image

from inkscape_mcp.config import ENV_MAX_EXPORT_PX, ENV_MAX_OUTPUT_BYTES, get_settings
from inkscape_mcp.registry import get_registry
from inkscape_mcp.render import inline
from inkscape_mcp.render.cli import (
    RenderResult,
    _next_frame_index,
    capture_frame,
    export_document,
    list_frames,
    render_preview,
)
from inkscape_mcp.render.profiles import create_icon_set, export_print_profile, export_web_profile
from inkscape_mcp.tools._result import inline_png
from inkscape_mcp.workspace import sandbox
from tests.test_export_files import ART, FakeInkscape, install_fake
from tests.tool_support import PNG, call, open_doc

PNG_MAGIC = b"\x89PNG"


@pytest.fixture
def fake_inkscape(monkeypatch: pytest.MonkeyPatch, tool_ws: Path) -> FakeInkscape:
    return install_fake(monkeypatch)


@pytest.fixture
def doc(tool_ws: Path) -> str:
    return open_doc(tool_ws, "art.svg", ART)


def _file(doc_id: str, rel: str) -> Path:
    return Path(get_registry().get(doc_id).root) / rel


def _frames_dir(doc_id: str, series: str = "run") -> Path:
    root = Path(get_registry().get(doc_id).root)
    return sandbox.artifacts_dir(root, doc_id) / "frames" / series


def _decode(res) -> Image.Image:
    (image,) = res.images
    assert image.mimeType == "image/png"
    return Image.open(io.BytesIO(base64.b64decode(image.data)))


# --- engine: previews ---


def test_preview_dims_location_and_content(fake_inkscape, doc) -> None:
    result = render_preview(doc, width_px=200)
    out = _file(doc, result.workspace_relative_path)
    assert out.read_bytes()[:4] == PNG_MAGIC
    head = out.read_bytes()[:24]
    assert (result.width_px, result.height_px) == struct.unpack(">II", head[16:24]) == (200, 160)
    assert not result.workspace_relative_path.startswith("/")
    assert result.workspace_relative_path.startswith(
        f".inkscape-mcp/documents/{doc}/artifacts/preview/"
    )
    assert result.format == "png" and result.stale is False
    assert result.opaque_px and result.all_blank is False
    assert "--export-area-page" in fake_inkscape.calls[-1]


def test_previews_never_clobber_each_other(fake_inkscape, doc) -> None:
    a = render_preview(doc, width_px=128)
    b = render_preview(doc, width_px=128)
    assert a.workspace_relative_path != b.workspace_relative_path
    assert _file(doc, a.workspace_relative_path).exists()
    assert _file(doc, b.workspace_relative_path).exists()


def test_preview_name_tag_is_sanitised_into_the_stem(fake_inkscape, doc) -> None:
    named = render_preview(doc, width_px=64, name="before")
    assert "preview-before-" in named.workspace_relative_path
    tricky = render_preview(doc, width_px=64, name="../a b")
    name = Path(tricky.workspace_relative_path).name
    assert name.startswith("preview-a-b-") and "/" not in name


def test_blank_page_is_reported(fake_inkscape, doc) -> None:
    fake_inkscape.blank = True
    result = render_preview(doc, width_px=40)
    assert (result.opaque_px, result.all_blank) == (0, True)


@pytest.mark.parametrize(
    "run",
    [
        render_preview,
        capture_frame,
        list_frames,
        lambda doc_id: export_document(doc_id, "png"),
        export_web_profile,
        create_icon_set,
        export_print_profile,
    ],
    ids=["preview", "frame", "frames", "export", "web", "icons", "print"],
)
def test_unknown_document_is_a_keyerror(fake_inkscape, tool_ws, run) -> None:
    with pytest.raises(KeyError):
        run("d_missing")


# --- engine: frame series ---


def test_next_frame_index_is_derived_from_the_filesystem(tmp_path: Path) -> None:
    assert _next_frame_index(tmp_path) == 1
    for name in (
        "frame-001.png",
        "frame-007.png",
        "frame-003-step.png",
        "notes.txt",
        "preview-1.png",
    ):
        (tmp_path / name).touch()
    assert _next_frame_index(tmp_path) == 8
    # a planted symlink named like a frame must not steer the numbering
    (tmp_path / "frame-099.png").symlink_to(tmp_path / "notes.txt")
    assert _next_frame_index(tmp_path) == 8


def test_frames_are_numbered_per_series(fake_inkscape, doc) -> None:
    a, b = capture_frame(doc), capture_frame(doc)
    assert [(f.series, f.frame_index) for f in (a, b)] == [("run", 1), ("run", 2)]
    assert a.workspace_relative_path.endswith("artifacts/frames/run/frame-001.png")
    assert b.workspace_relative_path.endswith("frame-002.png")
    other = capture_frame(doc, series="alpha")
    assert (other.series, other.frame_index) == ("alpha", 1)
    assert "artifacts/frames/alpha/" in other.workspace_relative_path


def test_frame_label_and_collisions_never_clobber(fake_inkscape, doc) -> None:
    labelled = capture_frame(doc, label="after-recolor")
    assert labelled.workspace_relative_path.endswith("frame-001-after-recolor.png")
    # a later capture continues past it
    assert capture_frame(doc).frame_index == 2
    # a pre-existing frame (as left by an earlier server run) is continued past, not overwritten
    series_dir = _frames_dir(doc)
    (series_dir / "frame-005.png").write_bytes(PNG)
    assert capture_frame(doc).frame_index == 6


def test_frame_series_is_sanitised_to_one_folder(fake_inkscape, doc, tool_ws) -> None:
    result = capture_frame(doc, series="../escape")
    assert (
        result.series == "escape" and "artifacts/frames/escape/" in result.workspace_relative_path
    )
    assert not (tool_ws.parent / "escape").exists()


def test_list_frames_orders_by_index_and_ignores_strangers(fake_inkscape, doc) -> None:
    empty = list_frames(doc, series="never_used")
    assert (empty.series, empty.frames) == ("never_used", [])
    for _ in range(3):
        capture_frame(doc, series="run3")
    (_frames_dir(doc, "run3") / "notes.txt").write_text("x")
    listing = list_frames(doc, series="run3")
    assert [f.frame_index for f in listing.frames] == [1, 2, 3]
    for frame in listing.frames:
        assert _file(doc, frame.workspace_relative_path).read_bytes()[:4] == PNG_MAGIC


def test_list_frames_skips_symlinks(fake_inkscape, doc, tool_ws) -> None:
    frames = _frames_dir(doc)
    frames.mkdir(parents=True)
    (frames / "frame-001.png").write_bytes(PNG)
    secret = tool_ws.parent / "secret.png"
    secret.write_bytes(b"secret")
    (frames / "frame-002.png").symlink_to(secret)
    assert [f.frame_index for f in list_frames(doc).frames] == [1]


# --- inline_png: when a PNG is embedded ---


@pytest.fixture
def stored_png(fake_inkscape, doc) -> tuple[str, str]:
    return doc, render_preview(doc, width_px=32).workspace_relative_path


def test_inline_png_embeds_the_artifact_bytes(stored_png) -> None:
    doc_id, rel = stored_png
    image = inline_png(doc_id, rel)
    assert image is not None and image.data == _file(doc_id, rel).read_bytes()


def test_inline_png_skipped_when_over_the_inline_threshold(stored_png, monkeypatch) -> None:
    monkeypatch.setattr(inline, "DEFAULT_INLINE_MAX_BYTES", 1)
    assert inline_png(*stored_png) is None


def test_inline_png_respects_the_hard_output_cap(stored_png, monkeypatch) -> None:
    monkeypatch.setenv(ENV_MAX_OUTPUT_BYTES, "1")
    get_settings.cache_clear()
    assert inline_png(*stored_png) is None


def test_inline_png_skips_missing_files_and_unknown_documents(stored_png) -> None:
    doc_id, _ = stored_png
    assert inline_png(doc_id, "no/such.png") is None
    assert inline_png("d_missing", "x.png") is None


# --- the `render` tool: inline PNG ---


def test_tool_attaches_a_png_whose_dims_match_the_json(tool_app, fake_inkscape, doc) -> None:
    res = call(tool_app, "render", {"doc_id": doc, "width_px": 120})
    img = _decode(res)
    assert img.size == (120, 96) == (res.data["width_px"], res.data["height_px"])
    assert res.data["target"] == "page"
    assert res.data["all_blank"] is False and "stale" not in res.data
    assert "workspace_relative_path" not in res.data
    assert "do not Read" in res.data["note"]
    # the artifact itself is kept server-side
    previews = sandbox.artifacts_dir(Path(get_registry().get(doc).root), doc) / "preview"
    assert len(list(previews.glob("*.png"))) == 1


def test_tool_reports_a_blank_render(tool_app, fake_inkscape, doc) -> None:
    fake_inkscape.blank = True
    assert call(tool_app, "render", {"doc_id": doc, "width_px": 40}).data["all_blank"] is True


def test_tool_too_large_to_inline_still_renders(tool_app, fake_inkscape, doc, monkeypatch) -> None:
    monkeypatch.setattr(inline, "DEFAULT_INLINE_MAX_BYTES", 1)
    res = call(tool_app, "render", {"doc_id": doc, "width_px": 64})
    assert res.images == [] and "lower width_px" in res.data["note"]
    assert res.data["width_px"] == 64


def test_tool_object_render_clips_to_the_object(tool_app, fake_inkscape, doc) -> None:
    res = call(tool_app, "render", {"doc_id": doc, "target": "object", "object_id": "dot"})
    assert res.data["target"] == "object" and len(res.images) == 1
    argv = fake_inkscape.calls[-1]
    assert "--export-id=dot" in argv and "--export-id-only" in argv


def test_tool_bad_object_ids_never_reach_inkscape(tool_app, fake_inkscape, doc) -> None:
    with pytest.raises(ToolError, match="object id is not valid"):
        call(tool_app, "render", {"doc_id": doc, "target": "object", "object_id": "id;rm -rf"})
    with pytest.raises(ToolError, match="object id not found in document"):
        call(tool_app, "render", {"doc_id": doc, "target": "object", "object_id": "ghost"})
    with pytest.raises(ToolError, match="requires object_id"):
        call(tool_app, "render", {"doc_id": doc, "target": "object"})
    assert fake_inkscape.calls == []


def test_tool_argument_combinations_are_checked(tool_app, fake_inkscape, doc) -> None:
    with pytest.raises(ToolError, match="only used with target='object'"):
        call(tool_app, "render", {"doc_id": doc, "object_id": "r1"})
    with pytest.raises(ToolError, match="only used with target='frame'"):
        call(tool_app, "render", {"doc_id": doc, "series": "x"})
    with pytest.raises(ToolError, match="document id not found"):
        call(tool_app, "render", {"doc_id": "nope"})
    assert fake_inkscape.calls == []


@pytest.mark.parametrize("target", ["page", "frame"])
def test_tool_pixel_cap_is_enforced_before_inkscape(
    tool_app, fake_inkscape, doc, monkeypatch, target
) -> None:
    monkeypatch.setenv(ENV_MAX_EXPORT_PX, "2")
    get_settings.cache_clear()
    with pytest.raises(ToolError, match="render exceeds the configured size or dimension limit"):
        call(tool_app, "render", {"doc_id": doc, "target": target, "width_px": 500})
    assert fake_inkscape.calls == []


def test_tool_frames_form_an_ordered_series(tool_app, fake_inkscape, doc) -> None:
    seen = [
        call(tool_app, "render", {"doc_id": doc, "target": "frame", "series": "s1", "label": "a"}),
        call(tool_app, "render", {"doc_id": doc, "target": "frame", "series": "s1"}),
        call(tool_app, "render", {"doc_id": doc, "target": "frame"}),
    ]
    assert [(r.data["series"], r.data["frame"]) for r in seen] == [("s1", 1), ("s1", 2), ("run", 1)]
    assert all(len(r.images) == 1 for r in seen)
    assert [f.name for f in list_frames(doc, "s1").frames] == ["frame-001-a.png", "frame-002.png"]


def test_tool_frame_width_must_be_positive(tool_app, fake_inkscape, doc) -> None:
    with pytest.raises(ToolError, match="render/export failed"):
        call(tool_app, "render", {"doc_id": doc, "target": "frame", "width_px": -5})
    assert fake_inkscape.calls == []


def test_tool_missing_engine_is_actionable_and_leaks_nothing(tool_app, fake_inkscape, doc) -> None:
    from inkscape_mcp.workspace.subprocess_exec import ProcessError

    fake_inkscape.error = ProcessError("inkscape binary not found")
    with pytest.raises(ToolError) as exc:
        call(tool_app, "render", {"doc_id": doc})
    assert "help(topic='diagnose')" in str(exc.value) and "binary not found" not in str(exc.value)


def _all_artifacts(doc_id: str) -> list[Path]:
    root = Path(get_registry().get(doc_id).root)
    return [p for p in sandbox.artifacts_dir(root, doc_id).rglob("*") if p.is_file()]


@pytest.mark.parametrize("knob", ["returncode", "timed_out"])
@pytest.mark.parametrize("target", ["page", "object", "frame"])
def test_tool_failed_or_timed_out_inkscape_leaves_no_artifact(
    tool_app, fake_inkscape, doc, knob, target
) -> None:
    setattr(fake_inkscape, knob, 1 if knob == "returncode" else True)
    args = {"doc_id": doc, "target": target, **({"object_id": "dot"} if target == "object" else {})}
    with pytest.raises(ToolError, match="render/export failed"):
        call(tool_app, "render", args)
    assert fake_inkscape.calls and _all_artifacts(doc) == []


@pytest.mark.parametrize("target", ["page", "object", "frame"])
def test_tool_oversized_preview_artifact_is_deleted_and_refused(
    tool_app, fake_inkscape, doc, monkeypatch, target
) -> None:
    monkeypatch.setenv(ENV_MAX_OUTPUT_BYTES, "20")  # every stub PNG is larger
    get_settings.cache_clear()
    args = {"doc_id": doc, "target": target, **({"object_id": "dot"} if target == "object" else {})}
    with pytest.raises(ToolError, match="render exceeds the configured size or dimension limit"):
        call(tool_app, "render", args)
    assert fake_inkscape.calls and _all_artifacts(doc) == []


# --- real renderer ---


@pytest.mark.inkscape
def test_real_preview_and_frames(doc) -> None:
    result = render_preview(doc, width_px=200)
    out = _file(doc, result.workspace_relative_path)
    w, h = struct.unpack(">II", out.read_bytes()[16:24])
    assert (result.width_px, result.height_px) == (w, h) == (200, 160)
    assert result.opaque_px and result.all_blank is False
    first = capture_frame(doc, series="demo", width_px=200)
    assert (first.frame_index, first.width_px, first.height_px) == (1, 200, 160)
    assert capture_frame(doc, series="demo").frame_index == 2


@pytest.mark.inkscape
def test_real_blank_document_renders_all_blank(tool_ws) -> None:
    blank = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 40 40"/>'
    doc_id = open_doc(tool_ws, "blank.svg", blank)
    result = render_preview(doc_id, width_px=40)
    assert result.opaque_px == 0 and result.all_blank is True


def _white_dot_png(path: Path) -> None:
    im = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    im.paste((255, 255, 255, 255), (5, 5, 15, 15))
    im.save(path)


@pytest.fixture
def white_dot(monkeypatch: pytest.MonkeyPatch, doc: str):
    """The render tool's page render replaced by a white dot on a transparent PNG."""
    import inkscape_mcp.tools.render as tool_render

    root = Path(get_registry().get(doc).root)
    out = root / "dot.png"
    _white_dot_png(out)
    monkeypatch.setattr(
        tool_render,
        "render_preview",
        lambda doc_id, width_px=None: RenderResult(
            doc_id=doc_id,
            workspace_relative_path="dot.png",
            format="png",
            width_px=20,
            height_px=20,
            duration_s=0.0,
        ),
    )
    return out


def test_render_background_fills_transparency(tool_app, doc, white_dot) -> None:
    res = call(tool_app, "render", {"doc_id": doc, "background": "#333"})
    assert "note" in res.data and "background=" not in res.data["note"]
    assert "backdrop" not in res.data["note"]  # an explicit background is never overridden
    px = _decode(res)
    assert px.convert("RGBA").getpixel((0, 0)) == (0x33, 0x33, 0x33, 255)
    assert px.convert("RGBA").getpixel((10, 10)) == (255, 255, 255, 255)


def test_white_only_render_gets_auto_backdrop(tool_app, doc, white_dot) -> None:
    res = call(tool_app, "render", {"doc_id": doc})
    assert "backdrop was added" in res.data["note"] and "transparent" in res.data["note"]
    px = _decode(res).convert("RGBA")
    assert px.getpixel((0, 0)) == (0x33, 0x33, 0x33, 255)
    assert px.getpixel((10, 10)) == (255, 255, 255, 255)


def test_render_notes_white_on_transparent(tool_app, doc, white_dot) -> None:
    res = call(tool_app, "render", {"doc_id": doc, "background": "none"})
    assert "background='#333'" in res.data["note"]
    assert _decode(res).convert("RGBA").getpixel((0, 0))[3] == 0  # opt-out: still transparent


@pytest.mark.parametrize("bad", ["red; rm -rf", "--export-id=x", "#12", "url(x)", "nosuchcolour"])
def test_render_background_is_validated(tool_app, doc, white_dot, bad) -> None:
    with pytest.raises(ToolError, match="background must be a CSS colour"):
        call(tool_app, "render", {"doc_id": doc, "background": bad})


@pytest.mark.inkscape
def test_real_white_circle_object_render(tool_app, tool_ws) -> None:
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 40 40">'
        b'<circle id="mark" cx="20" cy="20" r="15" fill="#fff"/></svg>'
    )
    doc_id = open_doc(tool_ws, "dot.svg", svg)
    args = {"doc_id": doc_id, "target": "object", "object_id": "mark"}
    auto = call(tool_app, "render", args)
    assert "backdrop was added" in auto.data["note"]
    assert _decode(auto).convert("RGBA").getpixel((0, 0))[:3] == (0x33, 0x33, 0x33)
    res = call(tool_app, "render", {**args, "background": "#333"})
    assert "backdrop" not in res.data["note"]
    assert _decode(res).convert("RGBA").getpixel((0, 0))[:3] == (0x33, 0x33, 0x33)
