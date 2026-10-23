"""Compose ops of `edit`: `set_svg` / `insert_svg` (allowlist, HIGH risk) and `place` / `grid`.

`set_svg` / `insert_svg` take an agent-written SVG string through the STRICT allowlist
(`edit.compose.parse_and_scrub`): no `<script>`, `on*` handlers, `javascript:` / external / data
hrefs, external `url()` paints or `<image>`. Both are HIGH risk and need an `approval_token`.
`place` / `grid` re-compose existing geometry (whole documents or single objects, deep-copied and
re-id'd) into the target. Hermetic: nothing here needs Inkscape.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from lxml import etree

from inkscape_mcp.config import get_settings
from inkscape_mcp.edit.compose import ComposeError, parse_and_scrub
from inkscape_mcp.edit.ops import OpError
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.validate import validate_document
from inkscape_mcp.workspace import sandbox
from tests.conftest import run_ops
from tests.tool_support import open_doc

TOKEN = "approve-123"
NS = 'xmlns="http://www.w3.org/2000/svg"'
XLINK = 'xmlns:xlink="http://www.w3.org/1999/xlink"'
SVG_NS = "{http://www.w3.org/2000/svg}"

BLANK = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100"/>'
TARGET = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
    b'<g id="layer1"><rect id="r1" x="0" y="0" width="4" height="4"/></g></svg>'
)
SOURCE = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20">'
    b'<circle id="dot" cx="10" cy="10" r="5" fill="#abc"/></svg>'
)


def _working(ws: Path, doc_id: str) -> etree._Element:
    return etree.fromstring(sandbox.working_copy(ws, doc_id).read_bytes())


def _by_id(root: etree._Element, oid: str) -> etree._Element | None:
    return next((e for e in root.iter() if isinstance(e.tag, str) and e.get("id") == oid), None)


def _text(ws: Path, doc_id: str) -> str:
    return sandbox.working_copy(ws, doc_id).read_text(encoding="utf-8")


@pytest.fixture
def doc(tool_ws: Path) -> str:
    return open_doc(tool_ws, "blank.svg", BLANK)


def _insert(svg: str, **kw: Any) -> dict[str, Any]:
    return {"op": "insert_svg", "svg": svg, **kw}


# --- the allowlist (engine level) -------------------------------------------

REJECTED = {
    "script": (f"<svg {NS}><script>alert(1)</script></svg>", "script"),
    "onload": (f'<svg {NS}><rect onload="x()"/></svg>', "event-handler"),
    "javascript-href": (
        f'<svg {NS} {XLINK}><use xlink:href="javascript:alert(1)"/></svg>',
        "external/active",
    ),
    "http-href": (f'<svg {NS} {XLINK}><use xlink:href="http://evil.example/x.svg"/></svg>', ""),
    "data-href": (f'<svg {NS} {XLINK}><use xlink:href="data:image/png;base64,AAAA"/></svg>', ""),
    "external-paint": (f'<svg {NS}><rect fill="url(http://evil/x)"/></svg>', ""),
    "image": (f'<svg {NS}><image href="x.png"/></svg>', "image"),
}
#: name -> (svg, tag, attribute, value the scrubbed tree must keep)
ALLOWED = {
    "same-document-href": (
        f'<svg {NS} {XLINK}><rect id="a" x="0" y="0" width="5" height="5"/>'
        '<use xlink:href="#a"/></svg>',
        "use",
        "{http://www.w3.org/1999/xlink}href",
        "#a",
    ),
    "fragment-paint": (
        f'<svg {NS}><defs><linearGradient id="g"><stop offset="0" stop-color="#fff"/>'
        '</linearGradient></defs><rect width="5" height="5" fill="url(#g)"/></svg>',
        "rect",
        "fill",
        "url(#g)",
    ),
}


@pytest.mark.parametrize("name", REJECTED)
def test_scrub_rejects(name: str) -> None:
    svg, fragment = REJECTED[name]
    with pytest.raises(ComposeError, match=fragment or None):
        parse_and_scrub(svg)


@pytest.mark.parametrize("name", ALLOWED)
def test_scrub_allows_same_document_references(name: str) -> None:
    svg, tag, attr, value = ALLOWED[name]
    elem = parse_and_scrub(svg).find(f".//{SVG_NS}{tag}")
    assert elem is not None and elem.get(attr) == value


def test_all_violations_listed_in_one_error() -> None:
    svg = f'<svg {NS} onload="x()"><script>alert(1)</script></svg>'
    with pytest.raises(ComposeError) as ei:
        parse_and_scrub(svg)
    assert str(ei.value) == (
        "svg contains a disallowed event-handler attribute: 'onload'; "
        "svg contains disallowed element: 'script'"
    )


def test_violation_list_deduped_and_capped() -> None:
    body = "<script/>" * 5
    body += "".join(f"<g data-{c}='1'/>" for c in "abcdefghijklmnop")
    with pytest.raises(ComposeError) as ei:
        parse_and_scrub(f"<svg {NS}>{body}</svg>")
    msg = str(ei.value)
    assert msg.count("'script'") == 1  # de-duplicated
    assert msg.endswith("more")
    assert len(msg.split("; ")) == 11  # 10 shown + "and more"


def test_violation_items_are_clipped() -> None:
    with pytest.raises(ComposeError) as ei:
        parse_and_scrub(f"<svg {NS}><{'x' * 5000}/></svg>")
    assert len(str(ei.value)) <= 200


# --- allowlist and approval through the ops -----------------------------------


@pytest.mark.parametrize("name", ["script", "onload", "image"])
@pytest.mark.parametrize("kw", [{"approval_token": TOKEN}, {}, {"dry_run": True}])
def test_set_svg_and_insert_svg_surface_the_allowlist_error(
    doc: str, tool_ws: Path, name, kw
) -> None:
    svg, fragment = REJECTED[name]
    before = _text(tool_ws, doc)
    with pytest.raises(OpError, match=rf"ops\[0\] set_svg: .*{fragment}"):
        run_ops(doc, {"op": "set_svg", "svg": svg}, **kw)
    with pytest.raises(OpError, match=rf"ops\[0\] insert_svg: .*{fragment}"):
        run_ops(
            doc,
            _insert(svg.replace("<svg", "<g", 1).replace("</svg>", "</g>")),
            **kw,
        )
    assert _text(tool_ws, doc) == before


@pytest.mark.parametrize("op", ["set_svg", "insert_svg"])
def test_compose_ops_are_high_risk_and_need_a_token(doc: str, tool_ws: Path, op: str) -> None:
    before = _text(tool_ws, doc)
    svg = (
        f'<svg {NS} width="10" height="10"/>' if op == "set_svg" else '<rect width="2" height="2"/>'
    )
    with pytest.raises(OpError, match=rf"ops\[0\] {op} is HIGH risk; re-call with approval_token"):
        run_ops(doc, {"op": op, "svg": svg})
    assert _text(tool_ws, doc) == before


def test_oversized_svg_is_rejected_before_parsing(doc: str, monkeypatch) -> None:
    monkeypatch.setenv("INKSCAPE_MCP_MAX_INPUT_BYTES", "1024")
    get_settings.cache_clear()
    pad = "<rect x='0' y='0' width='1' height='1'/>" * 60
    big = f'<svg {NS} viewBox="0 0 10 10">{pad}</svg>'
    with pytest.raises(OpError, match="size limit"):
        run_ops(doc, {"op": "set_svg", "svg": big}, approval_token=TOKEN)


# --- set_svg ------------------------------------------------------------------


def test_set_svg_replaces_the_document_and_it_validates(doc: str, tool_ws: Path) -> None:
    new = (
        f'<svg {NS} viewBox="0 0 20 20" width="20" height="20">'
        '<circle id="c1" cx="10" cy="10" r="5" fill="#ff0000"/></svg>'
    )
    res = run_ops(doc, {"op": "set_svg", "svg": new}, approval_token=TOKEN)
    assert res.changed and res.operation_id and res.snapshot_id and res.risk == "high"
    assert 'id="c1"' in _text(tool_ws, doc)
    assert validate_document(doc).ok


@pytest.mark.parametrize(
    ("svg", "message"),
    [
        ('<rect x="0" y="0" width="2" height="2"/>', "root element to be <svg>"),
        ("<svg><<not xml", "parsed safely"),
    ],
)
def test_set_svg_needs_a_parseable_svg_root(doc: str, svg: str, message: str) -> None:
    with pytest.raises(OpError, match=rf"ops\[0\] set_svg: .*{message}"):
        run_ops(doc, {"op": "set_svg", "svg": svg}, approval_token=TOKEN)


# --- insert_svg ---------------------------------------------------------------


def test_insert_under_the_root_validates_and_is_reversible(doc: str, tool_ws: Path) -> None:
    before = sandbox.working_copy(tool_ws, doc).read_bytes()
    res = run_ops(
        doc, _insert('<rect id="r9" x="1" y="1" width="3" height="3"/>'), approval_token=TOKEN
    )
    assert res.changed and 'id="r9"' in _text(tool_ws, doc)
    assert validate_document(doc).ok
    restore_snapshot(doc, res.snapshot_id)
    assert sandbox.working_copy(tool_ws, doc).read_bytes() == before


def test_insert_unwraps_a_wrapper_svg_by_default(doc: str, tool_ws: Path) -> None:
    wrapper = (
        f'<svg {NS}><rect id="w1" x="0" y="0" width="2" height="2"/>'
        '<circle id="w2" cx="5" cy="5" r="1"/></svg>'
    )
    run_ops(doc, _insert(wrapper), approval_token=TOKEN)
    text = _text(tool_ws, doc)
    assert 'id="w1"' in text and 'id="w2"' in text
    assert text.count("<svg") == 1  # the wrapper itself is dropped


def test_insert_unwrap_false_keeps_the_nested_svg(doc: str, tool_ws: Path) -> None:
    nested = f'<svg {NS} id="nested1"><rect id="n1" x="0" y="0" width="2" height="2"/></svg>'
    run_ops(doc, _insert(nested, unwrap=False), approval_token=TOKEN)
    text = _text(tool_ws, doc)
    assert 'id="nested1"' in text and 'id="n1"' in text
    assert text.count("<svg") == 2


def test_an_empty_svg_wrapper_is_rejected_only_when_unwrapping(doc: str, tool_ws: Path) -> None:
    with pytest.raises(OpError, match="empty"):
        run_ops(doc, _insert(f"<svg {NS}/>"), approval_token=TOKEN)
    run_ops(doc, _insert(f'<svg {NS} id="empty1"/>', unwrap=False), approval_token=TOKEN)
    text = _text(tool_ws, doc)
    assert 'id="empty1"' in text and text.count("<svg") == 2


def test_insert_into_a_parent_group_or_created_parent(tool_ws: Path) -> None:
    doc = open_doc(tool_ws, "t.svg", TARGET)
    res = run_ops(
        doc,
        {"op": "create_group", "id": "box"},
        _insert('<rect id="in-box" width="2" height="2"/>', parent="$0"),
        _insert('<rect id="in-layer" width="2" height="2"/>', parent="layer1"),
        approval_token=TOKEN,
    )
    working = _working(tool_ws, doc)
    assert _by_id(working, "in-box").getparent() is _by_id(working, "box")
    assert _by_id(working, "in-layer").getparent() is _by_id(working, "layer1")
    assert [c.id for c in res.created] == ["box", "in-box", "in-layer"]


def test_insert_into_an_unknown_parent_is_refused(doc: str, tool_ws: Path) -> None:
    before = _text(tool_ws, doc)
    with pytest.raises(OpError, match=r"ops\[0\] insert_svg: parent id 'nope' not found"):
        run_ops(doc, _insert('<rect width="3" height="3"/>', parent="nope"), approval_token=TOKEN)
    assert _text(tool_ws, doc) == before


# --- place --------------------------------------------------------------------


@pytest.fixture
def pair(tool_ws: Path) -> tuple[str, str]:
    return open_doc(tool_ws, "target.svg", TARGET), open_doc(tool_ws, "source.svg", SOURCE)


def test_place_a_whole_document_deep_copies_it_into_a_scaled_wrapper(
    pair: tuple[str, str], tool_ws: Path
) -> None:
    target, source = pair
    res = run_ops(target, {"op": "place", "source_doc_id": source, "x": 30, "y": 40, "scale": 2.0})
    assert res.changed and res.operation_id and res.snapshot_id
    placed = res.created[0].id
    wroot = _working(tool_ws, target)
    wrapper = _by_id(wroot, placed)
    assert wrapper is not None and wrapper.tag == f"{SVG_NS}g"
    transform = wrapper.get("transform", "")
    assert "translate(30,40)" in transform and "scale(2)" in transform
    # copied and re-id'd, not moved
    assert _by_id(wroot, "dot") is None
    assert len([e for e in wroot.iter() if (e.get("id") or "").startswith("dot-")]) == 1
    assert _by_id(_working(tool_ws, source), "dot") is not None


def test_place_a_single_object_at_the_default_scale(pair: tuple[str, str], tool_ws: Path) -> None:
    target, source = pair
    res = run_ops(
        target, {"op": "place", "source_doc_id": source, "source_id": "dot", "x": 0, "y": 0}
    )
    wrapper = _by_id(_working(tool_ws, target), res.created[0].id)
    assert wrapper is not None and "scale(" not in wrapper.get("transform", "")


@pytest.mark.parametrize(
    ("op", "message"),
    [
        ({"x": 0, "y": 0}, r"invalid ops: ops\[0\]\.place\.source_doc_id"),
        (
            {"source_doc_id": "SRC", "source_id": "nope", "x": 0, "y": 0},
            "source id 'nope' not found",
        ),
        ({"source_doc_id": "SRC", "x": 0, "y": 0, "scale": 0.0}, "scale must be a finite number"),
        ({"source_doc_id": "d_gone", "x": 0, "y": 0}, r"ops\[0\] place: document id not found"),
    ],
)
def test_place_refuses_bad_input_without_changing_the_target(
    pair: tuple[str, str], tool_ws: Path, op: dict[str, Any], message: str
) -> None:
    target, source = pair
    before = _text(tool_ws, target)
    op = {"op": "place", **{k: (source if v == "SRC" else v) for k, v in op.items()}}
    with pytest.raises(OpError, match=message):
        run_ops(target, op)
    assert _text(tool_ws, target) == before


def _css_doc(color: str) -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20">'
        f"<defs><style>.accent {{ fill: {color}; }}</style></defs>"
        f'<rect id="bar" class="accent" width="5" height="5"/></svg>'
    ).encode()


def _scoped_rules(root: etree._Element) -> dict[str, str]:
    """class name -> fill from every `<style>` rule in `root`."""
    css = "".join(st.text or "" for st in root.iter(f"{SVG_NS}style"))
    return {m[1]: m[2] for m in re.finditer(r"\.([\w-]+)\s*\{\s*fill:\s*(#\w+)", css)}


@pytest.mark.parametrize("single_object", [False, True], ids=["whole-doc", "source_id"])
def test_place_scopes_each_copys_css_classes(tool_ws: Path, single_object: bool) -> None:
    target = open_doc(tool_ws, "target.svg", TARGET)
    red = open_doc(tool_ws, "red.svg", _css_doc("#e63946"))
    yellow = open_doc(tool_ws, "yellow.svg", _css_doc("#e9c46a"))
    extra = {"source_id": "bar"} if single_object else {}
    for src in (red, yellow):
        run_ops(target, {"op": "place", "source_doc_id": src, "x": 0, "y": 0, **extra})
    wroot = _working(tool_ws, target)
    rules = _scoped_rules(wroot)
    assert "accent" not in rules and len(rules) == 2  # no shared global class
    bars = [e for e in wroot.iter(f"{SVG_NS}rect") if (e.get("id") or "").startswith("bar-")]
    assert [rules[b.get("class", "")] for b in bars] == ["#e63946", "#e9c46a"]


# --- grid ---------------------------------------------------------------------


def test_grid_lays_whole_documents_out_row_major(pair: tuple[str, str], tool_ws: Path) -> None:
    target, source = pair
    res = run_ops(
        target,
        {"op": "grid", "doc_ids": [source, source], "rows": 1, "cols": 2, "cell": 50, "gap": 5},
    )
    wroot = _working(tool_ws, target)
    cells = [e for e in wroot.iter(f"{SVG_NS}g") if (e.get("id") or "").startswith("cell-")]
    assert [c.get("id").split("-")[1:3] for c in cells] == [["0", "0"], ["0", "1"]]
    assert "translate(55,0)" in cells[1].get("transform", "")
    assert [c.id for c in res.created] == [c.get("id") for c in cells]


def test_grid_lays_objects_of_one_document_out(pair: tuple[str, str], tool_ws: Path) -> None:
    target, source = pair
    res = run_ops(
        target,
        {
            "op": "grid",
            "source_doc_id": source,
            "source_ids": ["dot"],
            "rows": 2,
            "cols": 2,
            "cell": 30,
        },
    )
    assert res.changed and res.created[0].id.startswith("cell-0-0-")


@pytest.mark.parametrize(
    ("grid", "message"),
    [
        ({"doc_ids": ["SRC"], "source_ids": ["dot"], "source_doc_id": "SRC"}, "EXACTLY ONE"),
        ({"source_ids": ["dot"]}, "requires source_doc_id"),
        ({"source_doc_id": "SRC", "source_ids": ["nope"]}, "object id not found"),
        ({"doc_ids": ["d_gone"]}, "document id not found"),
        ({"doc_ids": ["SRC", "SRC", "SRC"]}, "3 assets but only 2 cells"),
        ({"doc_ids": ["SRC"], "cell": 0}, "cell must be a finite number greater than 0"),
        ({"doc_ids": ["SRC"], "padding": 30}, "padding leaves no room"),
    ],
)
def test_grid_refuses_bad_input_without_changing_the_target(
    pair: tuple[str, str], tool_ws: Path, grid: dict[str, Any], message: str
) -> None:
    target, source = pair
    before = _text(tool_ws, target)
    fixed = {
        k: ([source if i == "SRC" else i for i in v] if isinstance(v, list) else v)
        for k, v in grid.items()
    }
    fixed = {k: (source if v == "SRC" else v) for k, v in fixed.items()}
    op = {"op": "grid", "rows": 1, "cols": 2, "cell": 50, **fixed}
    with pytest.raises(OpError, match=message):
        run_ops(target, op)
    assert _text(tool_ws, target) == before


def test_copies_keep_their_own_css_classes_and_id_rules() -> None:
    """Two copies of a doc with a <style> must not share class rules (last rule won before)."""
    from inkscape_mcp.edit.dom import deep_copy_with_new_ids

    src = etree.fromstring(
        '<svg xmlns="http://www.w3.org/2000/svg"><style>.accent, svg #dot { fill: red }'
        "@media print { .accent { fill: black } }</style>"
        '<rect class="accent big" id="dot"/></svg>'
    )
    sheet = etree.fromstring('<svg xmlns="http://www.w3.org/2000/svg"><g class="accent"/></svg>')
    first, _ = deep_copy_with_new_ids(src, sheet, None)
    sheet.append(first)
    second, _ = deep_copy_with_new_ids(src, sheet, None)
    for clone in (first, second):
        rect = clone.find(f"{SVG_NS}rect")
        css = clone.find(f"{SVG_NS}style").text
        cls = rect.get("class").split()[0]
        assert cls != "accent" and rect.get("class").endswith(" big")  # undefined class kept
        assert css.count(f".{cls}") == 2 and f"#{rect.get('id')}" in css
    assert first.find(f"{SVG_NS}rect").get("class") != second.find(f"{SVG_NS}rect").get("class")
    assert sheet.find(f"{SVG_NS}g").get("class") == "accent"  # the target's own class untouched


def test_a_copy_without_its_own_style_keeps_the_document_classes() -> None:
    from inkscape_mcp.edit.dom import deep_copy_with_new_ids

    root = etree.fromstring(
        '<svg xmlns="http://www.w3.org/2000/svg"><style>.accent { fill: red }</style>'
        '<rect class="accent" id="r"/></svg>'
    )
    clone, _ = deep_copy_with_new_ids(root.find(f"{SVG_NS}rect"), root, None)
    assert clone.get("class") == "accent"


def test_grid_sheet_reports_stale_source_after_source_edit(pair: tuple[str, str]) -> None:
    from inkscape_mcp.tools.inspect_ import _sections

    target, source = pair
    run_ops(target, {"op": "grid", "doc_ids": [source], "rows": 1, "cols": 1, "cell": 50})

    def summary() -> dict[str, Any]:
        return _sections(target, ["summary"], None, "summary", 50, None)["summary"]

    assert "stale_sources" not in summary()
    run_ops(source, {"op": "create_rect", "x": 0, "y": 0, "width": 1, "height": 1})
    assert summary()["stale_sources"] == [source]


def test_editing_a_placed_source_warns_until_the_sheet_no_longer_holds_the_copy(
    pair: tuple[str, str],
) -> None:
    target, source = pair
    placed = run_ops(target, {"op": "place", "source_doc_id": source, "x": 0, "y": 0})
    res = run_ops(source, {"op": "create_rect", "x": 0, "y": 0, "width": 1, "height": 1})
    assert any(target in w and "stale" in w for w in res.warnings)
    restore_snapshot(target, placed.snapshot_id)  # sheet back to pre-place: no copy left
    res = run_ops(source, {"op": "create_rect", "x": 2, "y": 0, "width": 1, "height": 1})
    assert not any(target in w for w in res.warnings)
