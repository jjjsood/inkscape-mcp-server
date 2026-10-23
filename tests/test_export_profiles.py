"""Export profiles: `render.profiles` (engine) and `export` profile targets (web / icons / print).

A profile is a fixed recipe over `export_document`; size validation happens before any Inkscape
call. The `inkscape` binary is faked (see `test_export_files.FakeInkscape`), which still lets the
real argv be asserted (print flags). Tests that need the real renderer carry `inkscape`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import get_settings
from inkscape_mcp.registry import get_registry
from inkscape_mcp.render.cli import export_document
from inkscape_mcp.render.profiles import (
    DEFAULT_ICON_SIZES,
    ProfileSizeError,
    create_icon_set,
    export_print_profile,
    export_web_profile,
)
from inkscape_mcp.workspace.paths import SandboxViolation
from tests.test_export_files import ART, FakeInkscape, install_fake
from tests.tool_support import call, open_doc

PNG_MAGIC = b"\x89PNG"


@pytest.fixture
def fake_inkscape(monkeypatch: pytest.MonkeyPatch, tool_ws: Path) -> FakeInkscape:
    return install_fake(monkeypatch)


@pytest.fixture
def doc(tool_ws: Path) -> str:
    return open_doc(tool_ws, "art.svg", ART)


def _file(doc_id: str, rel: str) -> Path:
    return Path(get_registry().get(doc_id).root) / rel


def _export(app, doc_id, targets, **kw):
    return call(app, "export", {"doc_id": doc_id, "targets": targets, **kw}).data


# --- web -----------------------------------------------------------------------------------


def test_web_default_is_one_png_then_the_svg(fake_inkscape, doc) -> None:
    result = export_web_profile(doc, width_px=256)
    assert result.profile == "web" and result.applied_settings == {}
    png, svg = result.artifacts
    assert (png.format, png.width_px, png.height_px) == ("png", 256, 205)
    assert (svg.format, svg.width_px) == ("svg", None)
    assert png.scale is None and png.requested_width_px == 256
    for art in result.artifacts:
        assert art.path == art.workspace_relative_path and not art.path.startswith("/")
        assert art.path.startswith(f".inkscape-mcp/documents/{doc}/")
        assert _file(doc, art.path).is_file()
    assert _file(doc, png.path).read_bytes()[:4] == PNG_MAGIC
    assert png.opaque_px and png.all_blank is False


def test_web_default_width_is_1024(fake_inkscape, doc) -> None:
    assert export_web_profile(doc).artifacts[0].width_px == 1024


def test_web_scales_give_a_density_ladder(fake_inkscape, doc) -> None:
    result = export_web_profile(doc, width_px=100, scales=[3, 1, 2, 2])
    pngs, svg = result.artifacts[:-1], result.artifacts[-1]
    assert svg.format == "svg"
    assert [(a.width_px, a.scale) for a in pngs] == [(100, 1), (200, 2), (300, 3)]
    assert len({a.workspace_relative_path for a in pngs}) == 3  # distinct files


def test_web_explicit_widths_win_over_scales_and_carry_no_scale(fake_inkscape, doc) -> None:
    result = export_web_profile(doc, width_px=999, widths=[256, 64, 64], scales=[2])
    pngs = result.artifacts[:-1]
    assert [a.width_px for a in pngs] == [64, 256]  # ascending, de-duplicated
    assert all(a.scale is None for a in pngs)
    assert [a.requested_width_px for a in pngs] == [64, 256]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"widths": [0]}, "web width must be a positive integer"),
        ({"widths": [-5]}, "web width must be a positive integer"),
        ({"widths": []}, "at least one width"),
        ({"width_px": 100, "scales": [0]}, "web scale must be a positive integer"),
        ({"width_px": 100, "scales": []}, "at least one scale"),
        ({"width_px": 0, "scales": [1]}, "web width must be a positive integer"),
        ({"width_px": -1}, "web width must be a positive integer"),
    ],
)
def test_web_invalid_sizes_are_refused_before_inkscape(fake_inkscape, doc, kwargs, message) -> None:
    with pytest.raises(ProfileSizeError, match=message):
        export_web_profile(doc, **kwargs)
    assert fake_inkscape.calls == []


# --- icons ---------------------------------------------------------------------------------


def test_icons_default_to_the_standard_ladder(fake_inkscape, doc) -> None:
    result = create_icon_set(doc)
    assert result.profile == "icon"
    assert [a.requested_size_px for a in result.artifacts] == list(DEFAULT_ICON_SIZES)
    for art in result.artifacts:
        assert (art.format, art.width_px) == ("png", art.requested_size_px)
        assert _file(doc, art.path).read_bytes()[:4] == PNG_MAGIC


def test_icons_keep_the_requested_order(fake_inkscape, doc) -> None:
    result = create_icon_set(doc, sizes=[96, 24])
    assert [a.requested_size_px for a in result.artifacts] == [96, 24]
    assert [a.width_px for a in result.artifacts] == [96, 24]


def test_icons_are_square_padded_transparent(fake_inkscape, doc) -> None:
    from PIL import Image

    for art in create_icon_set(doc, sizes=[16, 64]).artifacts:
        n = art.requested_size_px
        assert (art.width_px, art.height_px) == (n, n)
        with Image.open(_file(doc, art.path)) as im:
            assert im.size == (n, n)
            assert im.getpixel((n // 2, 0))[3] == 0  # padding row is transparent
        assert art.opaque_px


def test_icon_size_errors_name_their_cause(fake_inkscape, doc) -> None:
    too_big = get_settings().max_export_px + 1
    with pytest.raises(ProfileSizeError, match="exceeds the configured pixel cap") as over:
        create_icon_set(doc, sizes=[64, too_big])
    with pytest.raises(ProfileSizeError, match="must be a positive integer") as non_positive:
        create_icon_set(doc, sizes=[0])
    assert str(over.value) != str(non_positive.value)
    assert fake_inkscape.calls == []  # validated up front: no partial icon set


# --- print ---------------------------------------------------------------------------------


def test_print_is_one_vector_pdf_with_audited_settings(fake_inkscape, doc) -> None:
    result = export_print_profile(doc)
    assert result.profile == "print" and len(result.artifacts) == 1
    pdf = result.artifacts[0]
    assert pdf.path.endswith(".pdf") and not pdf.path.startswith("/")
    assert (pdf.format, pdf.width_px, pdf.height_px) == ("pdf", None, None)
    assert pdf.is_vector is True and pdf.fonts_outlined is True
    assert result.applied_settings == {"text_to_path": "true", "pdf_version": "1.4"}
    argv = fake_inkscape.calls[-1]
    assert "--export-text-to-path" in argv and "--export-pdf-version=1.4" in argv


def test_print_pdf_differs_from_a_plain_pdf_even_without_text(fake_inkscape, doc) -> None:
    plain = export_document(doc, "pdf")
    printed = export_print_profile(doc).artifacts[0]
    plain_bytes = _file(doc, plain.workspace_relative_path).read_bytes()
    print_bytes = _file(doc, printed.workspace_relative_path).read_bytes()
    assert (plain_bytes[:8], print_bytes[:8]) == (b"%PDF-1.5", b"%PDF-1.4")


# --- out_dir / name_prefix ----------------------------------------------------------------


def test_profiles_honour_out_dir_and_prefix(fake_inkscape, doc, tool_ws) -> None:
    web = export_web_profile(doc, width_px=64, out_dir="dist/web", name_prefix="brand")
    icons = create_icon_set(doc, sizes=[16, 32], out_dir="dist/icons", name_prefix="app")
    pdf = export_print_profile(doc, out_dir="dist/print", name_prefix="final").artifacts[0]
    for arts, folder, tag in (
        (web.artifacts, "dist/web/", "brand"),
        (icons.artifacts, "dist/icons/", "app"),
    ):
        assert arts
        for art in arts:
            assert art.path == art.workspace_relative_path and art.path.startswith(folder)
            assert tag in Path(art.path).name and (tool_ws / art.path).is_file()
    assert pdf.path.startswith("dist/print/") and "final" in Path(pdf.path).name
    assert (tool_ws / pdf.path).read_bytes()[:4] == b"%PDF"
    # the caller prefix is joined with the profile's own per-file key
    assert "brand-web-64w" in web.artifacts[0].path and "app-icon-16" in icons.artifacts[0].path
    assert "final-print" in pdf.path


@pytest.mark.parametrize("out_dir", ["../escape", "/etc", "../../escape"])
@pytest.mark.parametrize(
    "run",
    [
        lambda d, o: export_web_profile(d, width_px=16, out_dir=o),
        lambda d, o: create_icon_set(d, sizes=[16], out_dir=o),
        lambda d, o: export_print_profile(d, out_dir=o),
    ],
    ids=["web", "icons", "print"],
)
def test_out_dir_escape_is_rejected_before_inkscape(fake_inkscape, doc, run, out_dir) -> None:
    with pytest.raises(SandboxViolation, match="outside workspace"):
        run(doc, out_dir)
    assert fake_inkscape.calls == []


# --- through the `export` tool --------------------------------------------------------------


def test_tool_profiles_report_kind_and_dims(tool_app, fake_inkscape, doc) -> None:
    icons = _export(tool_app, doc, [{"kind": "profile", "name": "icons", "sizes": [16, 32]}])
    assert [(a["kind"], a["width_px"], a["height_px"]) for a in icons["artifacts"]] == [
        ("profile:icons", 16, 16),
        ("profile:icons", 32, 32),
    ]
    web = _export(
        tool_app, doc, [{"kind": "profile", "name": "web", "width_px": 100, "scales": [1, 2]}]
    )
    assert [(a["format"], a.get("width_px")) for a in web["artifacts"]] == [
        ("png", 100),
        ("png", 200),
        ("svg", None),
    ]
    printed = _export(tool_app, doc, [{"kind": "profile", "name": "print"}])["artifacts"][0]
    assert (printed["kind"], printed["format"]) == ("profile:print", "pdf")
    assert "width_px" not in printed and printed["bytes"] > 0


def test_tool_profile_out_dir_prefix_and_set(tool_app, fake_inkscape, tool_ws) -> None:
    a, b = open_doc(tool_ws, "a.svg", ART), open_doc(tool_ws, "b.svg", ART)
    data = _export(
        tool_app,
        [a, b],
        [{"kind": "profile", "name": "icons", "sizes": [16]}],
        out_dir="dist/icons",
    )
    assert {x["doc_id"] for x in data["artifacts"]} == {a, b}
    for art in data["artifacts"]:
        assert art["path"].startswith("dist/icons/") and art["doc_id"] in Path(art["path"]).name
        assert (tool_ws / art["path"]).is_file()


@pytest.mark.parametrize(
    ("target", "message"),
    [
        (
            {"kind": "profile", "name": "icons", "sizes": [0]},
            "icon size must be a positive integer",
        ),
        (
            {"kind": "profile", "name": "icons", "sizes": [10**9]},
            "exceeds the configured pixel cap",
        ),
        (
            {"kind": "profile", "name": "web", "widths": [-1]},
            "web width must be a positive integer",
        ),
        ({"kind": "profile", "name": "web", "scales": [0]}, "web scale must be a positive integer"),
        ({"kind": "profile", "name": "icons", "out_dir": "x"}, None),  # unknown field
        ({"kind": "profile", "name": "poster"}, None),  # unknown profile
    ],
)
def test_tool_invalid_profile_targets(tool_app, fake_inkscape, doc, target, message) -> None:
    with pytest.raises(ToolError, match=message):
        _export(tool_app, doc, [target])
    assert fake_inkscape.calls == []


def test_tool_profile_failure_names_doc_and_target(tool_app, fake_inkscape, doc) -> None:
    bad = {"kind": "profile", "name": "icons", "sizes": [0]}
    with pytest.raises(ToolError, match=rf"^{doc} targets\[1\] \(profile\): icon size must be"):
        _export(tool_app, doc, [{"kind": "file", "format": "svg"}, bad])


# --- real renderer -------------------------------------------------------------------------

_TEXT = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="80" viewBox="0 0 200 80">'
    b'<rect width="200" height="80" fill="#fff"/>'
    b'<text x="10" y="45" font-size="20" fill="#000">Print Me</text></svg>'
)


@pytest.mark.inkscape
def test_real_web_profile_png_and_svg(doc) -> None:
    result = export_web_profile(doc, width_px=256)
    png, svg = result.artifacts
    assert (png.format, png.width_px) == ("png", 256) and png.opaque_px and png.all_blank is False
    assert _file(doc, png.path).read_bytes()[:4] == PNG_MAGIC
    assert _file(doc, svg.path).read_bytes()[:64].lstrip().startswith((b"<?xml", b"<svg"))


@pytest.mark.inkscape
def test_real_print_profile_is_vector_and_differs_from_plain(tool_ws) -> None:
    doc_id = open_doc(tool_ws, "text.svg", _TEXT)
    plain = export_document(doc_id, "pdf")
    printed = export_print_profile(doc_id).artifacts[0]
    assert printed.is_vector is True and printed.fonts_outlined is True
    plain_bytes = _file(doc_id, plain.workspace_relative_path).read_bytes()
    print_bytes = _file(doc_id, printed.workspace_relative_path).read_bytes()
    assert plain_bytes != print_bytes and print_bytes[:8] == b"%PDF-1.4"


def test_profiles_default_next_to_a_save_as_in_the_same_call(tool_app, fake_inkscape, doc) -> None:
    res = _export(
        tool_app,
        doc,
        [
            {"kind": "profile", "name": "web", "width_px": 32},
            {"kind": "save_as", "dest_path": "dist/site/logo.svg"},
        ],
    )
    assert res["saved"]["path"] == "dist/site/logo.svg"
    assert res["artifacts"] and all(a["path"].startswith("dist/site/") for a in res["artifacts"])
    assert "save_as folder" in res["out_dir"]
    # an explicit out_dir still wins; no save_as falls back to the source file's folder
    res = _export(
        tool_app,
        doc,
        [
            {"kind": "profile", "name": "web", "width_px": 32},
            {"kind": "save_as", "dest_path": "dist/site/logo2.svg"},
        ],
        out_dir="other",
    )
    assert all(a["path"].startswith("other/") for a in res["artifacts"])
    res = _export(tool_app, doc, [{"kind": "profile", "name": "web", "width_px": 32}])
    assert all("/" not in a["path"] for a in res["artifacts"])
    assert "source file's folder" in res["out_dir"]
