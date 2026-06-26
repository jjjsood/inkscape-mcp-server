"""Text / object edit tool tests (E2-02, ADR-004 / ADR-005).

Hermetic: `render_preview` is monkeypatched in the pipeline module so no test invokes Inkscape.
The fake writes a tiny PNG-ish file into the deterministic preview path and returns a
`RenderResult`, mirroring how the real engine behaves (the pipeline then copies that file to an
operation-specific name).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from lxml import etree

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.document.inspect import INKSCAPE_NS, SVG_NS, XLINK_NS
from inkscape_mcp.edit import pipeline
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render.cli import RenderResult
from inkscape_mcp.server import mcp
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.tools.text_object import (
    duplicate_object,
    rename_object,
    replace_text,
    set_font,
)
from inkscape_mcp.workspace import sandbox

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" '
    b'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
    b'xmlns:xlink="http://www.w3.org/1999/xlink" width="10" height="10">'
    b'<text id="t1">old</text>'
    b'<g id="g1"><rect id="r1"/></g>'
    b'<use id="u1" href="#r1"/>'
    b"</svg>"
)

#: A minimal PNG signature so the written file is plausibly a raster (content is irrelevant here).
PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-preview"

_SVG = f"{{{SVG_NS}}}"
_INKSCAPE_LABEL = f"{{{INKSCAPE_NS}}}label"
_XLINK_HREF = f"{{{XLINK_NS}}}href"


@pytest.fixture
def doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path, Path]:
    """Open a fixture SVG; return (doc_id, owning_root, original_source_path)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    src = ws / "logo.svg"
    src.write_bytes(SVG)
    entry = get_registry().open_document(str(src))
    return entry.doc_id, ws, src


def _install_fake_render(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace `render_preview` in the pipeline module with a hermetic fake.

    Writes a deterministic `artifacts/preview/preview-auto.png` (the real engine's no-width name)
    and returns a `RenderResult` whose `artifact_path` is workspace-relative to it.
    """

    def fake_render_preview(
        doc_id: str, width_px: int | None = None, settings: object | None = None
    ) -> RenderResult:
        entry = get_registry().get(doc_id)
        root = Path(entry.root)
        preview_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        out = preview_dir / "preview-auto.png"
        out.write_bytes(PNG_BYTES)
        # E11-01 one-location contract: artifact_path is workspace-ROOT-relative (matches engine).
        rel = out.relative_to(root).as_posix()
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


def _working_root(root: Path, doc_id: str) -> etree._Element:
    """Parse the on-disk working copy and return its root element."""
    working = sandbox.working_copy(root, doc_id)
    return etree.fromstring(working.read_bytes())


def _operation_record(root: Path, doc_id: str, operation_id: str) -> dict:
    op_file = sandbox.operations_dir(root, doc_id) / f"{operation_id}.json"
    return json.loads(op_file.read_text())


def _find(root: etree._Element, object_id: str) -> etree._Element | None:
    for elem in root.iter():
        if isinstance(elem.tag, str) and elem.get("id") == object_id:
            return elem
    return None


# 1. replace_text ------------------------------------------------------------


def test_replace_text_changes_content_records_medium_op(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, src = doc
    _install_fake_render(monkeypatch)

    result = replace_text(doc_id, "t1", "new content")

    # The working copy's <text> now holds the new content (re-parse from disk).
    wroot = _working_root(root, doc_id)
    text_el = _find(wroot, "t1")
    assert text_el is not None
    assert text_el.text == "new content"

    # Operation Record is applied / medium with before+after previews linked.
    record = _operation_record(root, doc_id, result.operation_id)
    assert record["status"] == "applied"
    assert record["risk_class"] == "medium"
    assert set(record["previews"]) == {"before", "after"}
    assert result.preview_before is not None
    assert result.preview_after is not None

    # The ORIGINAL source file is byte-unchanged.
    assert src.read_bytes() == SVG


def test_replace_text_rejects_non_text_target(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError):
        replace_text(doc_id, "g1", "nope")


def test_replace_text_rejects_control_chars(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError):
        replace_text(doc_id, "t1", "bad\x00null")


# 2. set_font ----------------------------------------------------------------


def test_set_font_writes_style_properties(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)

    set_font(doc_id, ["t1"], family="Arial", size="12pt", weight="bold")

    wroot = _working_root(root, doc_id)
    text_el = _find(wroot, "t1")
    assert text_el is not None
    style = text_el.get("style") or ""
    assert "font-family:Arial" in style
    assert "font-size:12pt" in style
    assert "font-weight:bold" in style


def test_set_font_rejects_injection_family_no_change(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)

    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()

    with pytest.raises(ToolError):
        set_font(doc_id, ["t1"], family="Arial;fill:red{")

    # No change landed on the working copy.
    assert working.read_bytes() == before


def test_set_font_requires_a_field(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError):
        set_font(doc_id, ["t1"])


def test_set_font_requires_targets(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError):
        set_font(doc_id, [], family="Arial")


# 2b. set_font glyph coverage (E16-04) ---------------------------------------

# JP text in a Latin-only family is the run's only correctness gap: it "renders" via fontconfig
# substitution while the saved SVG names a non-covering family. These tests assert the apply-time
# coverage signal, using fonts known-present on this host (probed below) so they are hermetic to the
# coverage logic, not to a specific font shipping everywhere.
JP_TEXT_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50">'
    b'<text id="jp">\xe3\x81\x93\xe3\x82\x93\xe3\x81\xab\xe3\x81\xa1\xe3\x81\xaf</text>'
    b"</svg>"
)


def _font_installed(family: str) -> bool:
    """True iff `family` is genuinely installed (fc-match resolves to it, not a substitute)."""
    from inkscape_mcp.fonts.coverage import family_charset

    return family_charset(family) is not None


def _jp_doc(ws: Path) -> str:
    src = ws / "jp.svg"
    src.write_bytes(JP_TEXT_SVG)
    return get_registry().open_document(str(src)).doc_id


def test_set_font_flags_non_covering_family(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting a Latin-only family on JP text returns coverage_ok=False + the uncovered chars."""
    _, ws, _ = doc
    _install_fake_render(monkeypatch)
    if not _font_installed("Liberation Sans"):
        pytest.skip("Liberation Sans not installed on host")

    doc_id = _jp_doc(ws)
    result = set_font(doc_id, ["jp"], family="Liberation Sans")

    assert result.coverage_ok is False
    cov = [c for c in result.font_coverage if c.object_id == "jp"]
    assert len(cov) == 1
    assert cov[0].family == "Liberation Sans"
    # The Hiragana characters are reported as uncovered (none of them is whitespace/punctuation).
    assert cov[0].uncovered_chars == "こんにちは"
    # A cmap-verified covering family is suggested and it is NOT the non-covering one.
    assert cov[0].suggested_family is not None
    assert cov[0].suggested_family != "Liberation Sans"


def test_set_font_covering_family_is_clean(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting a CJK family that covers the JP text yields coverage_ok=True, no uncovered chars."""
    _, ws, _ = doc
    _install_fake_render(monkeypatch)
    if not _font_installed("Noto Sans CJK JP"):
        pytest.skip("Noto Sans CJK JP not installed on host")

    doc_id = _jp_doc(ws)
    result = set_font(doc_id, ["jp"], family="Noto Sans CJK JP")

    assert result.coverage_ok is True
    cov = [c for c in result.font_coverage if c.object_id == "jp"]
    assert len(cov) == 1
    assert cov[0].uncovered_chars == ""
    assert cov[0].suggested_family is None


def test_set_font_latin_text_no_false_positive(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Latin text in a Latin font reports clean coverage — no false flag on spaces/punctuation."""
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    if not _font_installed("Liberation Sans"):
        pytest.skip("Liberation Sans not installed on host")

    # The fixture's <text id="t1"> contains "old"; give it ASCII text with space + punctuation.
    replace_text(doc_id, "t1", "Hello, World! (123)")
    result = set_font(doc_id, ["t1"], family="Liberation Sans")

    assert result.coverage_ok is True
    cov = [c for c in result.font_coverage if c.object_id == "t1"]
    assert len(cov) == 1
    assert cov[0].uncovered_chars == ""


def test_set_font_returns_edit_result_fields(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SetFontResult is additive: all EditResult fields remain (back-compat for consumers)."""
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    result = set_font(doc_id, ["t1"], family="Arial", size="12pt")
    # EditResult contract still holds.
    assert result.doc_id == doc_id
    assert result.changed is True
    assert result.operation_id
    assert result.snapshot_id
    # And the additive coverage fields exist.
    assert isinstance(result.coverage_ok, bool)
    assert isinstance(result.font_coverage, list)


# 3. duplicate_object --------------------------------------------------------


def test_duplicate_object_inserts_unique_sibling_after_original(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)

    result = duplicate_object(doc_id, "g1")

    wroot = _working_root(root, doc_id)

    # Original g1 + r1 still present.
    g1 = _find(wroot, "g1")
    assert g1 is not None
    assert _find(wroot, "r1") is not None

    # The clone is the immediate next sibling of g1, with a NEW unique top id.
    parent = g1.getparent()
    assert parent is not None
    siblings = list(parent)
    clone = siblings[siblings.index(g1) + 1]
    clone_id = clone.get("id")
    assert clone_id is not None
    assert clone_id != "g1"

    # The clone's descendant rect was re-suffixed (not a duplicate of r1).
    clone_rect = clone.find(f"{_SVG}rect")
    assert clone_rect is not None
    assert clone_rect.get("id") != "r1"

    # No duplicate ids anywhere in the document.
    ids = [e.get("id") for e in wroot.iter() if isinstance(e.tag, str) and e.get("id")]
    assert len(ids) == len(set(ids))

    # The new top id is reported in the summary.
    assert clone_id in (result.summary or "")


def test_duplicate_object_with_explicit_new_id(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)

    duplicate_object(doc_id, "g1", new_id="g1_copy")
    wroot = _working_root(root, doc_id)
    assert _find(wroot, "g1_copy") is not None


def test_duplicate_object_rejects_used_new_id(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError):
        duplicate_object(doc_id, "g1", new_id="r1")


# 4. rename_object -----------------------------------------------------------


def test_rename_object_rewrites_references(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)

    # Rename the referenced rect r1 -> rect_renamed; the <use href="#r1"> must follow.
    rename_object(doc_id, "r1", new_id="rect_renamed")

    wroot = _working_root(root, doc_id)
    assert _find(wroot, "r1") is None
    assert _find(wroot, "rect_renamed") is not None

    use_el = _find(wroot, "u1")
    assert use_el is not None
    href = use_el.get(_XLINK_HREF) or use_el.get("href")
    assert href == "#rect_renamed"


def test_rename_object_sets_label(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)

    rename_object(doc_id, "g1", label="My Group")
    wroot = _working_root(root, doc_id)
    g1 = _find(wroot, "g1")
    assert g1 is not None
    assert g1.get(_INKSCAPE_LABEL) == "My Group"


def test_rename_object_rejects_used_new_id(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError):
        rename_object(doc_id, "g1", new_id="r1")


def test_rename_object_requires_a_field(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError):
        rename_object(doc_id, "g1")


# 5. Reversibility -----------------------------------------------------------


def test_edit_is_reversible_via_linked_snapshot(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    _install_fake_render(monkeypatch)

    working = sandbox.working_copy(root, doc_id)
    pre_edit_bytes = working.read_bytes()

    result = replace_text(doc_id, "t1", "changed")
    assert working.read_bytes() != pre_edit_bytes  # mutation landed

    # Restoring the linked pre-mutation snapshot returns the working copy to its pre-edit bytes.
    restore_snapshot(doc_id, result.snapshot_id)
    assert working.read_bytes() == pre_edit_bytes


# 6. Error mapping -----------------------------------------------------------


def test_unknown_doc_id_maps_to_document_not_found(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError) as exc:
        replace_text("d_nope", "t1", "x")
    assert "document id not found" in str(exc.value)


def test_missing_object_id_maps_to_object_not_found(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc
    _install_fake_render(monkeypatch)
    with pytest.raises(ToolError) as exc:
        replace_text(doc_id, "does_not_exist", "x")
    assert "object id not found in document" in str(exc.value)


# 7. Registration ------------------------------------------------------------


def test_tools_registered_on_mcp(doc: tuple[str, Path, Path]) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {"replace_text", "set_font", "duplicate_object", "rename_object"} <= names
