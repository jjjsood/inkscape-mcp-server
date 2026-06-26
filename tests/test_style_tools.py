"""Style tool + engine tests (ADR-004 / ADR-005).

Hermetic: the pipeline's preview rendering is monkeypatched so no Inkscape is invoked. Each test
asserts the working copy changes, the original source stays byte-identical, the Operation Record
and previews link correctly, and edits are reversible via the snapshot chain.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from lxml import etree

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render.cli import RenderResult
from inkscape_mcp.server import mcp
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.tools.style import (
    apply_palette,
    replace_color,
    set_fill,
    set_opacity,
    set_stroke,
)
from inkscape_mcp.workspace import sandbox

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
    b'<rect id="r1" fill="#ff0000"/>'
    b'<rect id="r2" style="fill:#00ff00"/>'
    b"</svg>"
)

SVG_NS = "http://www.w3.org/2000/svg"


def _fake_render_preview(doc_id: str, width_px: int | None = None) -> RenderResult:
    """Hermetic stand-in for `render_preview`: write a tiny deterministic PNG, return its result.

    Mirrors the real engine's contract: the file lands under `artifacts/preview/` with the
    deterministic `preview-<descriptor>.png` name and `artifact_path` is workspace-ROOT-relative
    (one-location contract), so the pipeline's `root / artifact_path` join resolves.
    """
    reg = get_registry()
    entry = reg.get(doc_id)
    root = Path(entry.root)
    preview_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    descriptor = "auto" if width_px is None else f"{width_px}px"
    produced = preview_dir / f"preview-{descriptor}.png"
    produced.write_bytes(b"\x89PNG\r\n\x1a\n")
    rel = produced.relative_to(root).as_posix()
    return RenderResult(
        doc_id=doc_id,
        artifact_path=rel,
        workspace_relative_path=rel,
        format="png",
        width_px=10,
        height_px=10,
        duration_s=0.0,
    )


@pytest.fixture
def doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path, Path]:
    """Open a fixture SVG with hermetic preview rendering; return (doc_id, root, source_path)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    monkeypatch.setattr("inkscape_mcp.edit.pipeline.render_preview", _fake_render_preview)
    src = ws / "logo.svg"
    src.write_bytes(SVG)
    entry = get_registry().open_document(str(src))
    return entry.doc_id, ws, src


def _working_root(doc_id: str, root: Path) -> etree._Element:
    working = sandbox.working_copy(root, doc_id)
    return etree.fromstring(working.read_bytes())


def _find(root: etree._Element, object_id: str) -> etree._Element:
    for elem in root.iter():
        if isinstance(elem.tag, str) and elem.get("id") == object_id:
            return elem
    raise AssertionError(f"id {object_id!r} not found")


def _style_props(elem: etree._Element) -> dict[str, str]:
    decls: dict[str, str] = {}
    for part in (elem.get("style") or "").split(";"):
        if ":" in part:
            key, value = part.split(":", 1)
            decls[key.strip()] = value.strip()
    return decls


# --- 1. set_fill mutates working DOM, links snapshot + previews + Operation Record ---


def test_set_fill_changes_working_copy_and_records_operation(
    doc: tuple[str, Path, Path],
) -> None:
    doc_id, root, _ = doc

    result = set_fill(doc_id, ["r1"], "#0000ff")

    # Working copy DOM reflects the new fill (written into the style block).
    r1 = _find(_working_root(doc_id, root), "r1")
    assert _style_props(r1)["fill"] == "#0000ff"

    # Snapshot + before/after previews are linked.
    assert result.changed is True
    assert result.snapshot_id.startswith("snap_")
    assert result.preview_before is not None
    assert result.preview_after is not None

    # The Operation Record records an applied medium-risk edit with both previews.
    op_file = sandbox.operations_dir(root, doc_id) / f"{result.operation_id}.json"
    record = json.loads(op_file.read_text())
    assert record["tool"] == "set_fill"
    assert record["status"] == "applied"
    assert record["risk_class"] == "medium"
    assert set(record["previews"]) == {"before", "after"}
    assert record["snapshot_id"] == result.snapshot_id


def test_set_fill_with_opacity_sets_fill_opacity(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    set_fill(doc_id, ["r1", "r2"], "blue", opacity=0.5)
    working = _working_root(doc_id, root)
    for oid in ("r1", "r2"):
        props = _style_props(_find(working, oid))
        assert props["fill"] == "blue"
        assert props["fill-opacity"] == "0.5"


# --- 2. The original source file is never mutated ---


def test_original_source_byte_unchanged_after_edit(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, src = doc
    set_fill(doc_id, ["r1"], "#123456")
    assert src.read_bytes() == SVG


# --- 3. Reversibility via the linked pre-mutation snapshot ---


def test_edit_is_reversible_via_snapshot(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()

    result = set_fill(doc_id, ["r1"], "#abcdef")
    assert working.read_bytes() != before

    restore_snapshot(doc_id, result.snapshot_id)
    assert working.read_bytes() == before


# --- 4. replace_color swaps inline-style AND presentation-attribute colours ---


def test_replace_color_swaps_inline_and_attribute(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc

    # r1 uses fill="#ff0000" (attribute); add a second target via shorthand to exercise color_key.
    result = replace_color(doc_id, "#f00", "#0000ff")  # 3-digit shorthand should match r1

    r1 = _find(_working_root(doc_id, root), "r1")
    assert _style_props(r1).get("fill") == "#0000ff" or r1.get("fill") == "#0000ff"
    assert "1 object" in (result.summary or "")

    # Now swap the inline-style green on r2.
    result2 = replace_color(doc_id, "#00FF00", "#000000")
    r2 = _find(_working_root(doc_id, root), "r2")
    assert _style_props(r2)["fill"] == "#000000"
    assert "1 object" in (result2.summary or "")


def test_apply_palette_applies_multiple_mappings(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    result = apply_palette(doc_id, {"#ff0000": "#111111", "#00ff00": "#222222"})
    working = _working_root(doc_id, root)
    r1 = _find(working, "r1")
    r2 = _find(working, "r2")
    assert (_style_props(r1).get("fill") or r1.get("fill")) == "#111111"
    assert _style_props(r2)["fill"] == "#222222"
    assert "2 mappings" in (result.summary or "")


# --- 5. CSS-injection guard ---


@pytest.mark.parametrize(
    "bad_color",
    ["red;stroke:url(#x)", "blue}", "#fff{color:red", "rgb(0,0,0);evil:1"],
)
def test_css_injection_color_rejected_no_change(
    doc: tuple[str, Path, Path], bad_color: str
) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()

    with pytest.raises(ToolError):
        set_fill(doc_id, ["r1"], bad_color)

    # No DOM change occurred (mutation is rejected before any write).
    assert working.read_bytes() == before


def test_set_stroke_requires_at_least_one_property(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, _ = doc
    with pytest.raises(ToolError):
        set_stroke(doc_id, ["r1"])


def test_set_opacity_out_of_range_rejected(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()
    with pytest.raises(ToolError):
        set_opacity(doc_id, ["r1"], 1.5)
    assert working.read_bytes() == before


def test_set_stroke_sets_provided_properties(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    set_stroke(doc_id, ["r1"], color="#333333", width="2px", opacity=0.25)
    props = _style_props(_find(_working_root(doc_id, root), "r1"))
    assert props["stroke"] == "#333333"
    assert props["stroke-width"] == "2px"
    assert props["stroke-opacity"] == "0.25"


# --- 6. Unknown doc / missing object id map to stable ToolErrors ---


def test_unknown_doc_id_maps_to_toolerror(doc: tuple[str, Path, Path]) -> None:
    with pytest.raises(ToolError) as exc:
        set_fill("d_nope", ["r1"], "#000000")
    assert "document id not found" in str(exc.value)


def test_missing_object_id_maps_to_toolerror(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, _ = doc
    with pytest.raises(ToolError) as exc:
        set_fill(doc_id, ["does-not-exist"], "#000000")
    assert "object id not found in document" in str(exc.value)


def test_empty_object_ids_rejected(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, _ = doc
    with pytest.raises(ToolError):
        set_fill(doc_id, [], "#000000")


# --- 8. Honest `changed` flag ---


def _op_files(root: Path, doc_id: str) -> int:
    op_dir = sandbox.operations_dir(root, doc_id)
    return len(list(op_dir.glob("op_*.json"))) if op_dir.exists() else 0


def _snap_files(root: Path, doc_id: str) -> int:
    snap_dir = sandbox.snapshots_dir(root, doc_id)
    return len(list(snap_dir.glob("*.svg"))) if snap_dir.exists() else 0


def test_replace_color_zero_matches_reports_changed_false(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()
    ops_before = _op_files(root, doc_id)
    snaps_before = _snap_files(root, doc_id)

    # No element uses #abcdef, so this matches nothing — a genuine no-op.
    result = replace_color(doc_id, "#abcdef", "#123123")

    assert result.changed is False
    assert result.operation_id == ""
    assert result.snapshot_id == ""
    assert "0 object" in (result.summary or "")
    # No-op writes NOTHING: no snapshot, no Operation Record, no working-copy change.
    assert _op_files(root, doc_id) == ops_before
    assert _snap_files(root, doc_id) == snaps_before
    assert working.read_bytes() == before


def test_set_fill_to_same_color_is_noop(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    # r2 already carries style="fill:#00ff00"; setting the SAME colour changes nothing.
    before = working.read_bytes()
    ops_before = _op_files(root, doc_id)
    snaps_before = _snap_files(root, doc_id)

    result = set_fill(doc_id, ["r2"], "#00ff00")

    assert result.changed is False
    assert result.operation_id == ""
    assert result.snapshot_id == ""
    assert _op_files(root, doc_id) == ops_before
    assert _snap_files(root, doc_id) == snaps_before
    assert working.read_bytes() == before


def test_real_style_change_writes_exactly_one_snapshot_and_record(
    doc: tuple[str, Path, Path],
) -> None:
    doc_id, root, _ = doc
    ops_before = _op_files(root, doc_id)
    snaps_before = _snap_files(root, doc_id)

    result = set_fill(doc_id, ["r1"], "#0000ff")

    assert result.changed is True
    assert result.operation_id.startswith("op_")
    assert result.snapshot_id.startswith("snap_")
    assert _op_files(root, doc_id) == ops_before + 1
    assert _snap_files(root, doc_id) == snaps_before + 1


def test_replace_color_real_match_reports_changed_true(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, _ = doc
    result = replace_color(doc_id, "#ff0000", "#0000ff")
    assert result.changed is True


def test_apply_palette_no_matches_reports_changed_false(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, _ = doc
    # Both colours are valid but appear nowhere in the document.
    result = apply_palette(doc_id, {"#aaaaaa": "#bbbbbb"})
    assert result.changed is False


# --- 9. apply_palette validates colours up front ---


def _op_count(root: Path, doc_id: str) -> int:
    op_dir = sandbox.operations_dir(root, doc_id)
    return len(list(op_dir.glob("*.json"))) if op_dir.exists() else 0


def test_apply_palette_invalid_value_raises_before_any_op(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    before_bytes = working.read_bytes()
    before_ops = _op_count(root, doc_id)

    # `notacolor` is letters-only but NOT a real CSS colour keyword — must be rejected.
    with pytest.raises(ToolError):
        apply_palette(doc_id, {"#3366cc": "notacolor"})

    # No mutation, and no Operation Record / snapshot was written for the rejected palette.
    assert working.read_bytes() == before_bytes
    assert _op_count(root, doc_id) == before_ops


def test_apply_palette_invalid_key_raises_before_any_op(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    before_ops = _op_count(root, doc_id)
    with pytest.raises(ToolError):
        apply_palette(doc_id, {"notacolor": "#000000"})
    assert _op_count(root, doc_id) == before_ops


def test_apply_palette_valid_named_colour_still_applies(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    # `blue` IS a real CSS named colour and must still be accepted and applied.
    result = apply_palette(doc_id, {"#ff0000": "blue"})
    r1 = _find(_working_root(doc_id, root), "r1")
    assert (_style_props(r1).get("fill") or r1.get("fill")) == "blue"
    assert result.changed is True


# --- 7. All five tools registered on the MCP app ---


def test_tools_registered_on_mcp(doc: tuple[str, Path, Path]) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    for tool_name in (
        "set_fill",
        "set_stroke",
        "set_opacity",
        "replace_color",
        "apply_palette",
    ):
        assert tool_name in names


# --- 8.: re-applying an existing value is a true no-op (no attr->style migration) ---


def _snapshot_count(doc_id: str, root: Path) -> int:
    return len(list(sandbox.snapshots_dir(root, doc_id).glob("*.svg")))


def test_set_fill_same_presentation_attr_value_is_noop(doc: tuple[str, Path, Path]) -> None:
    """Setting r1's fill to the colour it already carries as a PRESENTATION ATTRIBUTE
    (`fill="#ff0000"`) must be a genuine no-op: no attr->style rewrite, no snapshot, no op."""
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()
    snaps_before = _snapshot_count(doc_id, root)

    result = set_fill(doc_id, ["r1"], "#ff0000")

    assert result.changed is False
    assert result.operation_id == ""
    assert result.snapshot_id == ""
    assert result.preview_before is None and result.preview_after is None
    # Byte-identical working copy: the presentation attribute is NOT migrated into a style block.
    assert working.read_bytes() == before
    r1 = _find(_working_root(doc_id, root), "r1")
    assert r1.get("fill") == "#ff0000"
    assert "fill" not in _style_props(r1)
    # No snapshot was written.
    assert _snapshot_count(doc_id, root) == snaps_before


def test_set_fill_same_value_case_and_shorthand_insensitive_noop(
    doc: tuple[str, Path, Path],
) -> None:
    """A colour equal under color_key (case / 3-digit shorthand) is also a no-op on an attr."""
    doc_id, root, _ = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    result = set_fill(doc_id, ["r1"], "#F00")  # == #ff0000 under color_key
    assert result.changed is False
    assert sandbox.working_copy(root, doc_id).read_bytes() == before


def test_set_fill_inline_style_same_value_is_noop(doc: tuple[str, Path, Path]) -> None:
    """r2 already carries fill via inline style; re-applying the same value is a no-op too."""
    doc_id, root, _ = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    result = set_fill(doc_id, ["r2"], "#00ff00")
    assert result.changed is False
    assert sandbox.working_copy(root, doc_id).read_bytes() == before


def test_set_fill_different_value_still_changes_once(doc: tuple[str, Path, Path]) -> None:
    """Control: a genuinely different colour still mutates and records exactly one snapshot."""
    doc_id, root, _ = doc
    snaps_before = _snapshot_count(doc_id, root)
    result = set_fill(doc_id, ["r1"], "#0000ff")
    assert result.changed is True
    assert _snapshot_count(doc_id, root) == snaps_before + 1


# ---: url(#id) paint-server reference accepted by set_fill / set_stroke ---


def test_set_fill_accepts_url_paint_reference(doc: tuple[str, Path, Path]) -> None:
    """A `url(#id)` gradient/pattern reference is a valid fill (the gradient round-trip)."""
    doc_id, root, _ = doc
    result = set_fill(doc_id, ["r1"], "url(#grad1)")
    assert result.changed is True
    r1 = _find(_working_root(doc_id, root), "r1")
    assert _style_props(r1)["fill"] == "url(#grad1)"


def test_set_fill_url_paint_preserves_id_case_and_fallback(doc: tuple[str, Path, Path]) -> None:
    """The referenced id is preserved verbatim (case-sensitive) and a fallback colour is kept."""
    doc_id, root, _ = doc
    set_fill(doc_id, ["r1"], "url(#MyGrad) #ff0000")
    r1 = _find(_working_root(doc_id, root), "r1")
    assert _style_props(r1)["fill"] == "url(#MyGrad) #ff0000"


def test_set_stroke_accepts_url_paint_reference(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    set_stroke(doc_id, ["r1"], color="url(#grad1)")
    r1 = _find(_working_root(doc_id, root), "r1")
    assert _style_props(r1)["stroke"] == "url(#grad1)"


@pytest.mark.parametrize(
    "bad",
    [
        "url(http://evil.example/x)",  # external url — never fetched/written
        "url(#id);fill:red",  # CSS-injection punctuation
        "url(#id) javascript:alert(1)",  # bad fallback
        "javascript:alert(1)",
        "url(#bad id)",  # space in id (not the safe charset)
    ],
)
def test_set_fill_rejects_unsafe_paint(doc: tuple[str, Path, Path], bad: str) -> None:
    """sec.12: an external/javascript/injection paint value is rejected, nothing is written."""
    doc_id, root, _ = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    with pytest.raises(ToolError):
        set_fill(doc_id, ["r1"], bad)
    assert sandbox.working_copy(root, doc_id).read_bytes() == before
