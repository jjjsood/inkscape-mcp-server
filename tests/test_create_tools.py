"""Element-creation / defs / grouping tool tests (, ADR-004 / ADR-005).

Hermetic: `render_preview` is monkeypatched in the pipeline module so no test invokes Inkscape
(mirrors `test_text_object_tools.py`). Each test opens a fixture SVG, runs a creation tool, and
asserts the element/def/group landed on the on-disk working copy, the returned `object_id` + `bbox`
are correct, the change is reversible (linked snapshot), and `changed` is True. Rejection cases
assert a `ToolError` and (where relevant) that nothing landed on the working copy.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from lxml import etree

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.document.inspect import SVG_NS, XLINK_NS
from inkscape_mcp.edit import pipeline
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render.cli import RenderResult
from inkscape_mcp.server import mcp
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.tools.create import (
    add_linear_gradient,
    add_radial_gradient,
    create_circle,
    create_ellipse,
    create_group,
    create_line,
    create_path,
    create_polygon,
    create_polyline,
    create_rect,
    create_text,
    create_use,
    group_objects,
    reparent_object,
)
from inkscape_mcp.workspace import sandbox

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" '
    b'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
    b'xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="100">'
    b'<g id="layer1" inkscape:groupmode="layer">'
    b'<rect id="r1" x="0" y="0" width="4" height="4"/>'
    b'<circle id="c1" cx="5" cy="5" r="2"/>'
    b"</g>"
    b"</svg>"
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-preview"

_SVG = f"{{{SVG_NS}}}"
_XLINK_HREF = f"{{{XLINK_NS}}}"


@pytest.fixture
def doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path, Path]:
    """Open a fixture SVG; return (doc_id, owning_root, original_source_path)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    src = ws / "scene.svg"
    src.write_bytes(SVG)
    entry = get_registry().open_document(str(src))
    return entry.doc_id, ws, src


def _install_fake_render(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_render_preview(
        doc_id: str, width_px: int | None = None, settings: object | None = None
    ) -> RenderResult:
        entry = get_registry().get(doc_id)
        root = Path(entry.root)
        preview_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        out = preview_dir / "preview-auto.png"
        out.write_bytes(PNG_BYTES)
        rel = out.relative_to(root).as_posix()
        return RenderResult(
            doc_id=doc_id,
            artifact_path=rel,
            workspace_relative_path=rel,
            format="png",
            width_px=100,
            height_px=100,
            duration_s=0.01,
        )

    monkeypatch.setattr(pipeline, "render_preview", fake_render_preview)


def _working_root(root: Path, doc_id: str) -> etree._Element:
    working = sandbox.working_copy(root, doc_id)
    return etree.fromstring(working.read_bytes())


def _find(root: etree._Element, object_id: str) -> etree._Element | None:
    for elem in root.iter():
        if isinstance(elem.tag, str) and elem.get("id") == object_id:
            return elem
    return None


# --- shape primitives ----------------------------------------------


def test_create_rect_inserts_into_default_layer_with_bbox(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, src = doc
    _install_fake_render(monkeypatch)

    result = create_rect(doc_id, 10, 20, 30, 40)

    assert result.changed is True
    assert result.operation_id
    assert result.snapshot_id
    assert result.object_id
    assert result.bbox is not None
    assert (result.bbox.x, result.bbox.y, result.bbox.width, result.bbox.height) == (10, 20, 30, 40)

    wroot = _working_root(root, doc_id)
    rect = _find(wroot, result.object_id)
    assert rect is not None
    assert rect.tag == f"{_SVG}rect"
    # Default parent is the first inkscape layer.
    assert rect.getparent() is not None
    assert rect.getparent().get("id") == "layer1"
    # Original source is untouched.
    assert src.read_bytes() == SVG


def test_create_rect_with_explicit_parent_and_id(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)

    result = create_rect(doc_id, 0, 0, 5, 5, parent_id="layer1", object_id="mybox")
    assert result.object_id == "mybox"
    wroot = _working_root(root, doc_id)
    assert _find(wroot, "mybox") is not None


def test_create_circle_bbox(doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)
    result = create_circle(doc_id, 10, 10, 5)
    assert result.bbox is not None
    assert (result.bbox.x, result.bbox.y, result.bbox.width, result.bbox.height) == (5, 5, 10, 10)
    assert _find(_working_root(root, doc_id), result.object_id) is not None


def test_create_ellipse_bbox(doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    result = create_ellipse(doc_id, 10, 10, 4, 2)
    assert result.bbox is not None
    assert (result.bbox.x, result.bbox.y, result.bbox.width, result.bbox.height) == (6, 8, 8, 4)


def test_create_line_bbox(doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    result = create_line(doc_id, 1, 2, 9, 6)
    assert result.bbox is not None
    assert (result.bbox.x, result.bbox.y, result.bbox.width, result.bbox.height) == (1, 2, 8, 4)


def test_create_polygon_bbox(doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)
    result = create_polygon(doc_id, [(0, 0), (10, 0), (5, 8)])
    assert result.bbox is not None
    assert (result.bbox.x, result.bbox.y, result.bbox.width, result.bbox.height) == (0, 0, 10, 8)
    poly = _find(_working_root(root, doc_id), result.object_id)
    assert poly is not None
    assert poly.get("points") == "0,0 10,0 5,8"


def test_create_polyline_bbox(doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    result = create_polyline(doc_id, [(2, 2), (4, 8)])
    assert result.bbox is not None
    assert (result.bbox.x, result.bbox.y, result.bbox.width, result.bbox.height) == (2, 2, 2, 6)


def test_create_path_valid_d_no_bbox(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)
    result = create_path(doc_id, "M0,0 L10,10 C 5 5 6 6 7 7 Z")
    assert result.changed is True
    assert result.bbox is None
    path = _find(_working_root(root, doc_id), result.object_id)
    assert path is not None
    assert path.get("d") == "M0,0 L10,10 C 5 5 6 6 7 7 Z"


def test_create_text_no_bbox_text_node(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)
    result = create_text(doc_id, 5, 5, "Hello <b>world</b>")
    assert result.bbox is None
    txt = _find(_working_root(root, doc_id), result.object_id)
    assert txt is not None
    # Stored as a text node — no child markup injected.
    assert txt.text == "Hello <b>world</b>"
    assert len(list(txt)) == 0


# --- defs / gradients ----------------------------------------------


def test_add_linear_gradient_lands_in_defs(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)

    result = add_linear_gradient(
        doc_id,
        [{"offset": "0", "color": "#ffffff"}, {"offset": "100%", "color": "red", "opacity": 0.5}],
    )
    assert result.changed is True
    assert result.object_id
    assert result.bbox is None

    wroot = _working_root(root, doc_id)
    grad = _find(wroot, result.object_id)
    assert grad is not None
    assert grad.tag == f"{_SVG}linearGradient"
    # The gradient is inside <defs>.
    parent = grad.getparent()
    assert parent is not None
    assert parent.tag == f"{_SVG}defs"
    stops = grad.findall(f"{_SVG}stop")
    assert len(stops) == 2
    assert "stop-color:#ffffff" in (stops[0].get("style") or "")
    assert "stop-opacity:0.5" in (stops[1].get("style") or "")


def test_add_radial_gradient_lands_in_defs(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)
    result = add_radial_gradient(
        doc_id, [{"offset": "0", "color": "blue"}, {"offset": "1", "color": "black"}], fx="40%"
    )
    wroot = _working_root(root, doc_id)
    grad = _find(wroot, result.object_id)
    assert grad is not None
    assert grad.tag == f"{_SVG}radialGradient"
    assert grad.get("fx") == "40%"
    assert grad.getparent().tag == f"{_SVG}defs"


def test_gradient_rejects_injection_color(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()
    with pytest.raises(ToolError):
        add_linear_gradient(doc_id, [{"offset": "0", "color": "red;fill:url(#x){"}])
    assert working.read_bytes() == before


def test_gradient_rejects_bad_offset(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError):
        add_linear_gradient(doc_id, [{"offset": "2", "color": "red"}])


# --- grouping / symbols --------------------------------------------


def test_create_group_empty(doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)
    result = create_group(doc_id, parent_id="layer1")
    g = _find(_working_root(root, doc_id), result.object_id)
    assert g is not None
    assert g.tag == f"{_SVG}g"
    assert len(list(g)) == 0


def test_group_objects_wraps_existing(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)

    result = group_objects(doc_id, ["r1", "c1"])
    wroot = _working_root(root, doc_id)
    group = _find(wroot, result.object_id)
    assert group is not None
    assert group.tag == f"{_SVG}g"
    # Both objects are now children of the new group.
    child_ids = {c.get("id") for c in group}
    assert child_ids == {"r1", "c1"}
    # The group sits inside the original parent (layer1).
    assert group.getparent().get("id") == "layer1"
    # No duplicate ids.
    ids = [e.get("id") for e in wroot.iter() if isinstance(e.tag, str) and e.get("id")]
    assert len(ids) == len(set(ids))


def test_group_objects_rejects_empty(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError):
        group_objects(doc_id, [])


def test_group_objects_rejects_missing_target(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError) as exc:
        group_objects(doc_id, ["nope"])
    assert "object id not found in document" in str(exc.value)


def test_reparent_object_moves_under_new_parent(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)

    # First make an empty group, then move r1 into it.
    g = create_group(doc_id, parent_id="layer1", object_id="bucket")
    assert g.object_id == "bucket"
    result = reparent_object(doc_id, "r1", "bucket")
    assert result.object_id == "r1"

    wroot = _working_root(root, doc_id)
    r1 = _find(wroot, "r1")
    assert r1 is not None
    assert r1.getparent().get("id") == "bucket"


def test_reparent_rejects_descendant_parent(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    # layer1 contains r1; moving layer1 under r1 must be rejected.
    with pytest.raises(ToolError):
        reparent_object(doc_id, "layer1", "r1")


def test_reparent_rejects_missing_parent(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError) as exc:
        reparent_object(doc_id, "r1", "nope")
    assert "object id not found in document" in str(exc.value)


def test_create_use_references_existing(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)

    result = create_use(doc_id, "r1", x=10, y=20)
    wroot = _working_root(root, doc_id)
    use = _find(wroot, result.object_id)
    assert use is not None
    assert use.tag == f"{_SVG}use"
    assert use.get(f"{_XLINK_HREF}href") == "#r1"
    assert use.get("href") == "#r1"
    assert use.get("x") == "10"
    assert use.get("y") == "20"


def test_create_use_rejects_external_href(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()
    with pytest.raises(ToolError):
        create_use(doc_id, "https://evil.example/x.svg#a")
    assert working.read_bytes() == before


def test_create_use_rejects_javascript_href(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError):
        create_use(doc_id, "javascript:alert(1)")


def test_create_use_rejects_missing_href_target(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError):
        create_use(doc_id, "does_not_exist")


def test_create_use_accepts_valid_transform(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)
    result = create_use(doc_id, "r1", transform="translate(10,0) scale(2)")
    use = _find(_working_root(root, doc_id), result.object_id)
    assert use.get("transform") == "translate(10,0) scale(2)"


@pytest.mark.parametrize(
    "bad_transform",
    [
        "scale(2);fill:red",  # CSS-injection punctuation
        "evil(1)",  # function outside the SVG transform set
        "translate(10) <script>",  # markup
        "javascript:alert(1)",
    ],
)
def test_create_use_rejects_unsafe_transform(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch, bad_transform: str
) -> None:
    # sec.12 (review): a transform value is validated to be allowed transform functions only;
    # an injection/markup/foreign-function value is rejected and nothing is written.
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()
    with pytest.raises(ToolError):
        create_use(doc_id, "r1", transform=bad_transform)
    assert working.read_bytes() == before


# --- rejection / validation shared cases ------------------------------------


def test_create_rect_rejects_bad_id(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError):
        create_rect(doc_id, 0, 0, 5, 5, object_id="bad id;<x>")


def test_create_rect_rejects_used_id(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError):
        create_rect(doc_id, 0, 0, 5, 5, object_id="r1")


def test_create_rect_rejects_nonpositive_size(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError):
        create_rect(doc_id, 0, 0, 0, 5)


def test_create_path_rejects_bad_d(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()
    with pytest.raises(ToolError):
        create_path(doc_id, 'M0,0 L10,10"/><script>alert(1)</script>')
    assert working.read_bytes() == before


def test_create_rect_rejects_missing_parent(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError) as exc:
        create_rect(doc_id, 0, 0, 5, 5, parent_id="nope")
    assert "object id not found in document" in str(exc.value)


def test_create_unknown_doc_id(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError) as exc:
        create_rect("d_nope", 0, 0, 5, 5)
    assert "document id not found" in str(exc.value)


# --- reversibility ----------------------------------------------------------


def test_create_is_reversible_via_linked_snapshot(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)

    working = sandbox.working_copy(root, doc_id)
    pre = working.read_bytes()

    result = create_rect(doc_id, 1, 1, 2, 2)
    assert working.read_bytes() != pre  # mutation landed

    restore_snapshot(doc_id, result.snapshot_id)
    assert working.read_bytes() == pre


# --- registration -----------------------------------------------------------


def test_create_tools_registered_on_mcp(doc: tuple[str, Path, Path]) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    expected = {
        "create_rect",
        "create_circle",
        "create_ellipse",
        "create_line",
        "create_polygon",
        "create_polyline",
        "create_path",
        "create_text",
        "add_linear_gradient",
        "add_radial_gradient",
        "create_group",
        "group_objects",
        "reparent_object",
        "create_use",
    }
    assert expected <= names
