"""Compose-engine + tool tests (E14-03): `set_document_svg` / `insert_svg_fragment`.

Covers the STRICT allowlist (reject `<script>`, `on*` handlers, `javascript:`/external hrefs),
the safe round-trip (valid fragment inserted + reversible via the pre-mutation snapshot), the HIGH
-risk approval gate, and the auto-run `validate_document` findings folded into the return.

Hermetic: `pipeline.render_preview` is monkeypatched so no test launches Inkscape (the before/after
preview frames are faked, the rest of the reversible pipeline runs for real).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.edit import pipeline
from inkscape_mcp.edit.compose import ComposeError, parse_and_scrub
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render.cli import RenderResult
from inkscape_mcp.server import mcp
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.tools.compose import insert_svg_fragment, set_document_svg
from inkscape_mcp.tools.document import create_document
from inkscape_mcp.workspace import sandbox

TOKEN = "approve-123"
PNG_BYTES = b"\x89PNG\r\n\x1a\nFAKE"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    return ws


@pytest.fixture(autouse=True)
def fake_render(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic `render_preview` (no Inkscape) for the pipeline's before/after frames."""

    def fake_render_preview(
        doc_id: str, width_px: int | None = None, settings: object | None = None
    ) -> RenderResult:
        entry = get_registry().get(doc_id)
        rt = Path(entry.root)
        preview_dir = sandbox.artifacts_dir(rt, doc_id) / "preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        out = preview_dir / "preview-auto.png"
        out.write_bytes(PNG_BYTES)
        rel = out.relative_to(rt).as_posix()
        return RenderResult(
            doc_id=doc_id,
            artifact_path=rel,
            workspace_relative_path=rel,
            format="png",
            width_px=10,
            height_px=10,
            duration_s=0.01,
        )

    monkeypatch.setattr(pipeline, "render_preview", fake_render_preview)


@pytest.fixture
def doc(root: Path) -> str:
    """A blank tracked document to compose into."""
    return create_document(width=100, height=100).doc_id


# --- allowlist rejections (engine level: parse_and_scrub) -------------------


def test_scrub_rejects_script() -> None:
    bad = '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    with pytest.raises(ComposeError) as exc:
        parse_and_scrub(bad)
    assert "script" in str(exc.value)


def test_scrub_rejects_onload_handler() -> None:
    bad = '<svg xmlns="http://www.w3.org/2000/svg"><rect onload="x()"/></svg>'
    with pytest.raises(ComposeError) as exc:
        parse_and_scrub(bad)
    assert "event-handler" in str(exc.value)


def test_scrub_rejects_javascript_href() -> None:
    bad = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<use xlink:href="javascript:alert(1)"/></svg>'
    )
    with pytest.raises(ComposeError) as exc:
        parse_and_scrub(bad)
    assert "external/active" in str(exc.value) or "javascript" in str(exc.value)


def test_scrub_rejects_external_http_href() -> None:
    bad = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<use xlink:href="http://evil.example/x.svg"/></svg>'
    )
    with pytest.raises(ComposeError):
        parse_and_scrub(bad)


def test_scrub_rejects_data_uri_href() -> None:
    bad = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<use xlink:href="data:image/png;base64,AAAA"/></svg>'
    )
    with pytest.raises(ComposeError):
        parse_and_scrub(bad)


def test_scrub_rejects_external_url_paint() -> None:
    bad = '<svg xmlns="http://www.w3.org/2000/svg"><rect fill="url(http://evil/x)"/></svg>'
    with pytest.raises(ComposeError):
        parse_and_scrub(bad)


def test_scrub_rejects_image_element() -> None:
    bad = '<svg xmlns="http://www.w3.org/2000/svg"><image href="x.png"/></svg>'
    with pytest.raises(ComposeError) as exc:
        parse_and_scrub(bad)
    assert "image" in str(exc.value)


def test_scrub_allows_same_document_fragment_href() -> None:
    ok = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<rect id="a" x="0" y="0" width="5" height="5"/>'
        '<use xlink:href="#a"/></svg>'
    )
    root = parse_and_scrub(ok)
    assert root is not None


def test_scrub_allows_url_fragment_paint() -> None:
    ok = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<defs><linearGradient id="g"><stop offset="0" stop-color="#fff"/>'
        "</linearGradient></defs>"
        '<rect x="0" y="0" width="5" height="5" fill="url(#g)"/></svg>'
    )
    assert parse_and_scrub(ok) is not None


# --- tool-level: allowlist rejections surface as ToolError ------------------


def test_set_document_svg_script_rejected_toolerror(doc: str) -> None:
    bad = '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    with pytest.raises(ToolError) as exc:
        set_document_svg(doc, bad, approval_token=TOKEN)
    assert "script" in str(exc.value)


def test_insert_fragment_onclick_rejected_toolerror(doc: str) -> None:
    with pytest.raises(ToolError) as exc:
        insert_svg_fragment(doc, '<rect onclick="x()"/>', approval_token=TOKEN)
    assert "event-handler" in str(exc.value)


# --- HIGH-risk approval gate ------------------------------------------------


def test_set_document_svg_requires_approval_token(doc: str) -> None:
    valid = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" width="10" height="10"/>'
    with pytest.raises(ToolError) as exc:
        set_document_svg(doc, valid, approval_token=None)
    assert "approval" in str(exc.value).lower()


def test_insert_fragment_empty_token_refused(doc: str) -> None:
    with pytest.raises(ToolError) as exc:
        insert_svg_fragment(doc, '<rect x="0" y="0" width="2" height="2"/>', approval_token="")
    assert "approval" in str(exc.value).lower()


# --- input-size cap (sec.12 DoS guard, E14 review) --------------------------


def test_set_document_svg_oversized_rejected_before_parse(
    doc: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An SVG string larger than `max_input_bytes` is rejected up front (no unbounded parse)."""
    monkeypatch.setenv("INKSCAPE_MCP_MAX_INPUT_BYTES", "1024")
    get_settings.cache_clear()
    # ~2 KiB of valid markup — well-formed, but over the 1 KiB cap.
    pad = "<rect x='0' y='0' width='1' height='1'/>" * 60
    big = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">{pad}</svg>'
    with pytest.raises(ToolError) as exc:
        set_document_svg(doc, big, approval_token=TOKEN)
    assert "size limit" in str(exc.value).lower()


# --- happy path: adopt + reversible + validation inline ---------------------


def test_set_document_svg_replaces_and_validates(doc: str) -> None:
    new = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" width="20" height="20">'
        '<circle id="c1" cx="10" cy="10" r="5" fill="#ff0000"/></svg>'
    )
    result = set_document_svg(doc, new, approval_token=TOKEN)
    assert result.changed
    assert result.operation_id
    assert result.snapshot_id
    # Validation findings folded in inline.
    assert result.validation.ok
    entry = get_registry().get(doc)
    working = Path(entry.working_path).read_text(encoding="utf-8")
    assert 'id="c1"' in working


def test_insert_svg_fragment_under_root(doc: str) -> None:
    result = insert_svg_fragment(
        doc, '<rect id="r9" x="1" y="1" width="3" height="3"/>', approval_token=TOKEN
    )
    assert result.changed
    entry = get_registry().get(doc)
    working = Path(entry.working_path).read_text(encoding="utf-8")
    assert 'id="r9"' in working
    assert result.validation.ok


def test_insert_fragment_default_unwraps_svg_wrapper(doc: str) -> None:
    """Default (`unwrap=True`): a wrapper <svg> is unwrapped, children grafted, wrapper dropped."""
    wrapper = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<rect id="w1" x="0" y="0" width="2" height="2"/>'
        '<circle id="w2" cx="5" cy="5" r="1"/>'
        "</svg>"
    )
    result = insert_svg_fragment(doc, wrapper, approval_token=TOKEN)
    assert result.changed
    entry = get_registry().get(doc)
    working = Path(entry.working_path).read_text(encoding="utf-8")
    # Children grafted in...
    assert 'id="w1"' in working
    assert 'id="w2"' in working
    # ...and no nested <svg> wrapper survives (only the document root <svg> remains).
    assert working.count("<svg") == 1


def test_insert_fragment_unwrap_false_keeps_nested_svg(doc: str) -> None:
    """`unwrap=False`: the wrapper <svg> is KEPT as a single nested <svg> under the parent."""
    wrapper = (
        '<svg xmlns="http://www.w3.org/2000/svg" id="nested1">'
        '<rect id="n1" x="0" y="0" width="2" height="2"/>'
        "</svg>"
    )
    result = insert_svg_fragment(doc, wrapper, unwrap=False, approval_token=TOKEN)
    assert result.changed
    entry = get_registry().get(doc)
    working = Path(entry.working_path).read_text(encoding="utf-8")
    # Nested <svg> retained (document root + the inserted nested one) plus its child.
    assert 'id="nested1"' in working
    assert 'id="n1"' in working
    assert working.count("<svg") == 2


def test_insert_fragment_empty_svg_wrapper_rejected_when_unwrapping(doc: str) -> None:
    """Default unwrap rejects an empty <svg> wrapper (nothing to graft)."""
    empty = '<svg xmlns="http://www.w3.org/2000/svg"/>'
    with pytest.raises(ToolError) as exc:
        insert_svg_fragment(doc, empty, approval_token=TOKEN)
    assert "empty" in str(exc.value).lower()


def test_insert_fragment_unwrap_false_allows_empty_nested_svg(doc: str) -> None:
    """`unwrap=False`: an empty nested <svg> is allowed (inserted verbatim as requested)."""
    empty = '<svg xmlns="http://www.w3.org/2000/svg" id="empty1"/>'
    result = insert_svg_fragment(doc, empty, unwrap=False, approval_token=TOKEN)
    assert result.changed
    entry = get_registry().get(doc)
    working = Path(entry.working_path).read_text(encoding="utf-8")
    assert 'id="empty1"' in working
    assert working.count("<svg") == 2


def test_insert_fragment_unknown_parent_rejected(doc: str) -> None:
    with pytest.raises(ToolError) as exc:
        insert_svg_fragment(
            doc,
            '<rect x="1" y="1" width="3" height="3"/>',
            parent_id="nope",
            approval_token=TOKEN,
        )
    assert "not found" in str(exc.value)


def test_compose_is_reversible(doc: str) -> None:
    entry = get_registry().get(doc)
    before = Path(entry.working_path).read_bytes()
    result = insert_svg_fragment(
        doc, '<rect id="rx" x="2" y="2" width="4" height="4"/>', approval_token=TOKEN
    )
    after = Path(entry.working_path).read_bytes()
    assert after != before
    restore_snapshot(doc, result.snapshot_id)
    assert Path(entry.working_path).read_bytes() == before


def test_set_document_svg_unknown_doc(root: Path) -> None:
    valid = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" width="10" height="10"/>'
    with pytest.raises(ToolError) as exc:
        set_document_svg("d_nope", valid, approval_token=TOKEN)
    assert "not found" in str(exc.value)


def test_set_document_svg_rejects_non_svg_root(doc: str) -> None:
    with pytest.raises(ToolError) as exc:
        set_document_svg(doc, '<rect x="0" y="0" width="2" height="2"/>', approval_token=TOKEN)
    assert "svg root" in str(exc.value).lower() or "<svg>" in str(exc.value)


def test_set_document_svg_unparseable(doc: str) -> None:
    with pytest.raises(ToolError) as exc:
        set_document_svg(doc, "<svg><<not xml", approval_token=TOKEN)
    assert "parsed safely" in str(exc.value)


def test_compose_tools_registered(root: Path) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "set_document_svg" in names
    assert "insert_svg_fragment" in names
