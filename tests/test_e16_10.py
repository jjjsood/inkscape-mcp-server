"""assorted authoring / live ergonomics: six independent sub-items (a-f).

Each test group locks one sub-item:

- (a) `place_document` re-composes existing geometry cross-doc (deep-copy/re-id reuse).
- (b) `create_*` accept optional `fill`/`stroke`/`stroke_width`, applied in the one call.
- (c) `stroke_to_path` engine drops the empty marker container when there is no marker.
- (d) `resize_canvas(bleed=…)` paints the bled strip in one call.
- (e) registered prompts are listed/readable via the `inkscape://prompts` resource surface.
- (f) `live_arm_socket` installs + arms the socket helper; the GUI launch leg is gated/skipped on
      a headless host with a clear marker (Tier-B style), so only the launch/helper path and
      the headless guard are asserted here.

Async is run via `asyncio.run(...)` inside sync test functions (repo convention).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from lxml import etree

from inkscape_mcp.config import ENV_LIVE_ENABLED, ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.edit.dom import parse_style
from inkscape_mcp.edit.paths import STROKE_TO_PATH, apply_path_op
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.server import mcp, register_tools
from inkscape_mcp.tools.compose import place_document
from inkscape_mcp.tools.create import (
    create_circle,
    create_line,
    create_path,
    create_rect,
    create_text,
)
from inkscape_mcp.tools.transform import resize_canvas
from inkscape_mcp.workspace import sandbox

register_tools()

_SVG = "{http://www.w3.org/2000/svg}"

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" '
    b'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
    b'width="100" height="100" viewBox="0 0 100 100">'
    b'<g id="layer1" inkscape:groupmode="layer">'
    b'<rect id="r1" x="0" y="0" width="4" height="4"/>'
    b"</g>"
    b"</svg>"
)

SVG_SOURCE = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20">'
    b'<circle id="dot" cx="10" cy="10" r="5" fill="#abc"/>'
    b"</svg>"
)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate a workspace root + registry for one test."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    return ws


def _open(ws: Path, name: str, body: bytes) -> str:
    src = ws / name
    src.write_bytes(body)
    return get_registry().open_document(str(src)).doc_id


def _working_root(doc_id: str) -> etree._Element:
    entry = get_registry().get(doc_id)
    working = sandbox.working_copy(Path(entry.root), doc_id)
    return etree.fromstring(working.read_bytes())


def _find(root: etree._Element, oid: str) -> etree._Element | None:
    for elem in root.iter():
        if isinstance(elem.tag, str) and elem.get("id") == oid:
            return elem
    return None


# --- (a) place_document -----------------------------------------------------


def test_place_whole_source_document_into_target(workspace: Path) -> None:
    target = _open(workspace, "target.svg", SVG)
    source = _open(workspace, "source.svg", SVG_SOURCE)

    result = place_document(target, x=30, y=40, source_doc_id=source, scale=2.0)

    assert result.changed is True
    assert result.operation_id and result.snapshot_id
    assert result.placed_id
    assert result.target_doc_id == target
    assert result.source == source

    wroot = _working_root(target)
    wrapper = _find(wroot, result.placed_id)
    assert wrapper is not None
    assert wrapper.tag == f"{_SVG}g"
    # Translate + scale folded onto the wrapper transform.
    transform = wrapper.get("transform", "")
    assert "translate(30,40)" in transform
    assert "scale(2)" in transform
    # The source object was deep-copied (re-id'd), not moved — the original id is NOT present here,
    # a suffixed clone is.
    assert _find(wroot, "dot") is None
    clones = [
        e for e in wroot.iter() if isinstance(e.tag, str) and (e.get("id") or "").startswith("dot-")
    ]
    assert len(clones) == 1

    # Source working copy is untouched: it still has the original id.
    assert _find(_working_root(source), "dot") is not None


def test_place_single_object_from_source(workspace: Path) -> None:
    target = _open(workspace, "target.svg", SVG)
    source = _open(workspace, "source.svg", SVG_SOURCE)

    result = place_document(target, x=0, y=0, source_doc_id=source, object_id="dot")
    assert result.changed is True
    wroot = _working_root(target)
    wrapper = _find(wroot, result.placed_id)
    assert wrapper is not None
    # scale==1 default -> no scale() in the transform, only the translate.
    assert "scale(" not in wrapper.get("transform", "")


def test_place_document_rejects_missing_source(workspace: Path) -> None:
    target = _open(workspace, "target.svg", SVG)
    with pytest.raises(ToolError):
        place_document(target, x=0, y=0)


def test_place_document_rejects_bad_object(workspace: Path) -> None:
    target = _open(workspace, "target.svg", SVG)
    source = _open(workspace, "source.svg", SVG_SOURCE)
    with pytest.raises(ToolError):
        place_document(target, x=0, y=0, source_doc_id=source, object_id="nope")


def test_place_document_rejects_bad_scale(workspace: Path) -> None:
    target = _open(workspace, "target.svg", SVG)
    source = _open(workspace, "source.svg", SVG_SOURCE)
    with pytest.raises(ToolError):
        place_document(target, x=0, y=0, source_doc_id=source, scale=0.0)


# --- (b) inline style on create ---------------------------------------------


def test_create_rect_inline_style_applied(workspace: Path) -> None:
    doc_id = _open(workspace, "scene.svg", SVG)
    result = create_rect(doc_id, 0, 0, 10, 10, fill="#3366cc", stroke="black", stroke_width="2")
    elem = _find(_working_root(doc_id), result.object_id)
    assert elem is not None
    style = parse_style(elem)
    assert style.get("fill") == "#3366cc"
    assert style.get("stroke") == "black"
    assert style.get("stroke-width") == "2"


def test_create_circle_inline_fill_only(workspace: Path) -> None:
    doc_id = _open(workspace, "scene.svg", SVG)
    result = create_circle(doc_id, 10, 10, 5, fill="red")
    elem = _find(_working_root(doc_id), result.object_id)
    assert elem is not None and parse_style(elem).get("fill") == "red"


def test_create_text_inline_fill(workspace: Path) -> None:
    doc_id = _open(workspace, "scene.svg", SVG)
    result = create_text(doc_id, 5, 5, "hi", fill="#111")
    elem = _find(_working_root(doc_id), result.object_id)
    assert elem is not None
    assert parse_style(elem).get("fill") == "#111"
    # Text content preserved alongside the style.
    assert elem.text == "hi"


def test_create_line_stroke_only(workspace: Path) -> None:
    doc_id = _open(workspace, "scene.svg", SVG)
    result = create_line(doc_id, 0, 0, 9, 9, stroke="blue", stroke_width="3")
    elem = _find(_working_root(doc_id), result.object_id)
    assert elem is not None
    style = parse_style(elem)
    assert style.get("stroke") == "blue" and style.get("stroke-width") == "3"


def test_create_default_no_style(workspace: Path) -> None:
    """Default None -> no style block written (additive, prior behaviour preserved)."""
    doc_id = _open(workspace, "scene.svg", SVG)
    result = create_rect(doc_id, 0, 0, 10, 10)
    elem = _find(_working_root(doc_id), result.object_id)
    assert elem is not None
    assert "fill" not in parse_style(elem)
    assert elem.get("style") in (None, "")


def test_create_inline_fill_accepts_url_paint(workspace: Path) -> None:
    doc_id = _open(workspace, "scene.svg", SVG)
    result = create_rect(doc_id, 0, 0, 10, 10, fill="url(#grad)")
    elem = _find(_working_root(doc_id), result.object_id)
    assert elem is not None
    assert parse_style(elem).get("fill") == "url(#grad)"


def test_create_inline_style_rejects_bad_color(workspace: Path) -> None:
    doc_id = _open(workspace, "scene.svg", SVG)
    with pytest.raises(ToolError):
        create_path(doc_id, "M0 0 L10 0", fill="#zzz; evil:1")


# --- (c) stroke_to_path empty marker ----------------------------------------


def _engine_output_with_empty_marker() -> bytes:
    """Simulated `object-stroke-to-path` plain-SVG output: a converted path PLUS an empty marker.

    Mirrors what Inkscape 1.4.x emits for a stroked object that carries no marker — the converted
    target path, and an empty `<g inkscape:label="markers">` (a no-op stub) alongside it.
    """
    return (
        b'<svg xmlns="http://www.w3.org/2000/svg" '
        b'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        b'width="100" height="100" viewBox="0 0 100 100">'
        b'<g id="layer1" inkscape:groupmode="layer">'
        b'<path id="r1" d="M0 0 L4 0 L4 4 Z" stroke="#000"/>'
        b'<g id="empty-markers" inkscape:label="markers"></g>'
        b'<path id="empty-marker-path"/>'
        b"</g>"
        b"</svg>"
    )


def test_stroke_to_path_drops_empty_marker(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id = _open(workspace, "scene.svg", SVG)
    entry = get_registry().get(doc_id)
    tree = etree.ElementTree(_working_root(doc_id))

    monkeypatch.setattr(
        "inkscape_mcp.edit.paths.run_path_op",
        lambda *a, **k: _engine_output_with_empty_marker(),
    )

    apply_path_op(tree, Path(entry.working_path), STROKE_TO_PATH, ["r1"])

    root = tree.getroot()
    # The converted target path survives; the empty marker containers are gone.
    assert _find(root, "r1") is not None
    assert _find(root, "empty-markers") is None
    assert _find(root, "empty-marker-path") is None


def test_stroke_to_path_keeps_real_marker(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A NON-empty marker container (real subtree) is never dropped."""
    doc_id = _open(workspace, "scene.svg", SVG)
    entry = get_registry().get(doc_id)
    tree = etree.ElementTree(_working_root(doc_id))

    def out(*_a: object, **_k: object) -> bytes:
        return (
            b'<svg xmlns="http://www.w3.org/2000/svg" '
            b'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
            b'width="100" height="100" viewBox="0 0 100 100">'
            b'<g id="layer1" inkscape:groupmode="layer">'
            b'<path id="r1" d="M0 0 L4 0 L4 4 Z"/>'
            b'<g id="real-markers"><path id="m" d="M0 0 L1 1"/></g>'
            b"</g>"
            b"</svg>"
        )

    monkeypatch.setattr("inkscape_mcp.edit.paths.run_path_op", out)
    apply_path_op(tree, Path(entry.working_path), STROKE_TO_PATH, ["r1"])
    root = tree.getroot()
    assert _find(root, "real-markers") is not None
    assert _find(root, "m") is not None


# --- (d) resize_canvas bleed ------------------------------------------------


def test_resize_canvas_bleed_paints_strip(workspace: Path) -> None:
    doc_id = _open(workspace, "scene.svg", SVG)
    result = resize_canvas(doc_id, "108", "108", bleed=4, bleed_color="#ffffff")
    assert result.changed is True
    root = _working_root(doc_id)
    # viewBox grown by 4 on every side: 0 0 100 100 -> -4 -4 108 108.
    assert root.get("viewBox") == "-4 -4 108 108"
    # A single background rect covering the extended box, painted white, behind content.
    bgs = [
        e
        for e in root.iter()
        if isinstance(e.tag, str)
        and etree.QName(e).localname == "rect"
        and (e.get("id") or "").startswith("bleed-bg")
    ]
    assert len(bgs) == 1
    bg = bgs[0]
    assert bg.get("fill") == "#ffffff"
    assert bg.get("x") == "-4" and bg.get("y") == "-4"
    assert bg.get("width") == "108" and bg.get("height") == "108"


def test_resize_canvas_no_bleed_unchanged_behaviour(workspace: Path) -> None:
    """Default (no bleed) leaves no background rect and preserves the existing viewBox."""
    doc_id = _open(workspace, "scene.svg", SVG)
    resize_canvas(doc_id, "200", "200")
    root = _working_root(doc_id)
    assert root.get("viewBox") == "0 0 100 100"
    assert not any(
        isinstance(e.tag, str) and (e.get("id") or "").startswith("bleed-bg") for e in root.iter()
    )


def test_resize_canvas_bleed_rejects_adjust_viewbox(workspace: Path) -> None:
    doc_id = _open(workspace, "scene.svg", SVG)
    with pytest.raises(ToolError):
        resize_canvas(doc_id, "108", "108", adjust_viewbox=True, bleed=4)


def test_resize_canvas_bleed_rejects_negative(workspace: Path) -> None:
    doc_id = _open(workspace, "scene.svg", SVG)
    with pytest.raises(ToolError):
        resize_canvas(doc_id, "108", "108", bleed=-1)


# --- (e) prompts in the resource surface ------------------------------------


def test_prompts_index_resource_lists_registered_prompts() -> None:
    async def read() -> str:
        async with Client(mcp) as client:
            uris = [str(r.uri) for r in await client.list_resources()]
            assert "inkscape://prompts" in uris
            contents = await client.read_resource("inkscape://prompts")
            return contents[0].text  # type: ignore[union-attr]

    payload = json.loads(asyncio.run(read()))
    names = {p["name"] for p in payload["prompts"]}
    assert payload["prompt_count"] == len(payload["prompts"])
    # The shipped prompt library is discoverable through the resource surface.
    assert {"live_canvas_assist", "compose_artwork", "prepare_web_export"} <= names
    # Argument descriptors are surfaced so a caller can invoke via prompts/get.
    compose = next(p for p in payload["prompts"] if p["name"] == "compose_artwork")
    assert any(a["name"] == "goal" and a["required"] for a in compose["arguments"])


# --- (f) live socket auto-arm -----------------------------------------------


def test_live_arm_socket_reuses_existing_session(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a session already advertises a socket, arm reuses it (no new launch, no GUI needed)."""
    from inkscape_mcp.live import socket_backend
    from inkscape_mcp.runtime.probe import Capabilities
    from inkscape_mcp.tools import live as live_tools
    from inkscape_mcp.tools import system as system_tools

    monkeypatch.setenv(ENV_LIVE_ENABLED, "1")
    get_settings.cache_clear()

    caps = Capabilities.model_construct(
        user_data_dir=str(workspace / "inkscape"), system_data_dir=None
    )
    # `live_arm_socket` imports `get_cached_capabilities` lazily from `tools.system`.
    monkeypatch.setattr(system_tools, "get_cached_capabilities", lambda: caps)
    # Pretend the helper is already installed and a rendezvous is already advertised.
    monkeypatch.setattr(live_tools, "is_helper_installed", lambda dirs: True)
    rv = socket_backend.Rendezvous(port=5000, token="tok")
    monkeypatch.setattr(socket_backend, "discover_rendezvous", lambda s: rv)

    result = live_tools.live_arm_socket()
    assert result.armed is True
    assert result.launched is False
    assert result.helper_installed is True


def test_arm_socket_helper_headless_skips_gui_launch(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier-B: the GUI launch leg is gated on a display; headless raises a clear error.

    This documents what's covered (helper install + launch-arg path + headless guard) vs deferred
    (the actual GUI socket arm, which cannot be verified headless on this host).
    """
    from inkscape_mcp.live import socket_backend

    # No advertising session, an inkscape binary present, but NO display available.
    monkeypatch.setattr(socket_backend, "discover_rendezvous", lambda s: None)
    monkeypatch.setattr(socket_backend.shutil, "which", lambda name: "/usr/bin/inkscape")
    monkeypatch.setattr(socket_backend, "_display_available", lambda: False)

    with pytest.raises(socket_backend.SocketArmError) as exc:
        socket_backend.arm_socket_helper(get_settings())
    assert "display" in str(exc.value).lower()


def test_arm_launch_argv_is_fixed_arglist() -> None:
    """The launch argv splices ONLY the server binary, the fixed action token, and a server path."""
    from inkscape_mcp.live import socket_backend

    argv = socket_backend._arm_launch_argv("/usr/bin/inkscape", Path("/tmp/doc.svg"))
    assert argv[0] == "/usr/bin/inkscape"
    assert f"--actions={socket_backend.HELPER_ACTION_ID}" in argv
    assert "--with-gui" in argv
    # No shell metacharacters / no client value: a plain, fixed arg list.
    assert all(isinstance(a, str) for a in argv)
