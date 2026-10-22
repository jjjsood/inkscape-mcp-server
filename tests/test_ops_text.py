"""Text ops of `edit`: create_text, replace_text, font (including glyph-coverage warnings).

`font` reports a non-covering family as a warning on the (still applied) edit: Japanese text in a
Latin-only family only "renders" through fontconfig substitution while the saved SVG names a family
that cannot draw it. The wiring is tested hermetically against a faked cmap; the host-font cases
skip when the font is not installed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from lxml import etree

from inkscape_mcp.edit import text_object
from inkscape_mcp.edit.ops import OpError
from inkscape_mcp.fonts.coverage import family_charset, suggest_covering_family
from inkscape_mcp.workspace import sandbox
from tests.conftest import run_ops
from tests.tool_support import open_doc

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
    b'<text id="t1">old</text><g id="g1"><rect id="r1"/></g>'
    b"</svg>"
)
JP = "こんにちは"
_NS = 'xmlns="http://www.w3.org/2000/svg"'
JP_SVG = f'<svg {_NS} width="100" height="50"><text id="jp">{JP}</text></svg>'


@pytest.fixture
def doc(tool_ws: Path) -> str:
    return open_doc(tool_ws, "logo.svg", SVG)


def _working(root: Path, doc_id: str) -> bytes:
    return sandbox.working_copy(root, doc_id).read_bytes()


def _elem(root: Path, doc_id: str, oid: str) -> etree._Element:
    tree = etree.fromstring(_working(root, doc_id))
    return next(e for e in tree.iter() if isinstance(e.tag, str) and e.get("id") == oid)


def _rejected(root: Path, doc_id: str, *ops: dict[str, Any], match: str | None = None) -> None:
    before = _working(root, doc_id)
    with pytest.raises(OpError, match=match):
        run_ops(doc_id, *ops)
    assert _working(root, doc_id) == before


def _installed(family: str) -> bool:
    """True iff `family` is genuinely installed (fc-match resolves to it, not a substitute)."""
    return family_charset(family) is not None


# --- create_text ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "match"),
    [("bad\x00null", "control characters"), ("x" * 100_001, "text too long")],
)
def test_create_text_rejects_unsafe_content(tool_ws, doc, text, match) -> None:
    _rejected(tool_ws, doc, {"op": "create_text", "x": 0, "y": 0, "text": text}, match=match)


# --- replace_text --------------------------------------------------------------------


def test_replace_text_changes_content_in_one_medium_record(tool_ws, doc) -> None:
    result = run_ops(doc, {"op": "replace_text", "ids": ["t1"], "text": "new content"})
    assert _elem(tool_ws, doc, "t1").text == "new content"
    (rec,) = [
        json.loads(p.read_text()) for p in sandbox.operations_dir(tool_ws, doc).glob("op_*.json")
    ]
    assert (rec["status"], rec["risk_class"]) == ("applied", "medium")
    assert rec["snapshot_id"] == result.snapshot_id
    # the source file is never touched
    assert (tool_ws / "logo.svg").read_bytes() == SVG


def test_replace_text_collapses_tspans_to_one_run(tool_ws) -> None:
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        b'<text id="t"><tspan id="a">one</tspan><tspan id="b">two</tspan></text></svg>'
    )
    doc_id = open_doc(tool_ws, "ts.svg", svg)
    run_ops(doc_id, {"op": "replace_text", "ids": ["t"], "text": "<b>x</b>"})
    text = _elem(tool_ws, doc_id, "t")
    # markup is stored as text, and the child runs are gone
    assert text.text == "<b>x</b>" and len(text) == 0


def test_replace_text_keeps_an_inkscape_single_line_tspan(tool_ws) -> None:
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        b'<text id="t" x="1" y="5"><tspan id="line" x="1" y="5" style="font-weight:bold">old'
        b"</tspan></text></svg>"
    )
    doc_id = open_doc(tool_ws, "line.svg", svg)
    run_ops(doc_id, {"op": "replace_text", "ids": ["t"], "text": "new"})
    line = _elem(tool_ws, doc_id, "line")
    assert line.text == "new" and line.get("style") == "font-weight:bold"
    assert not (_elem(tool_ws, doc_id, "t").text or "").strip()


def test_replace_text_accepts_a_selector(tool_ws, doc) -> None:
    run_ops(doc, {"op": "replace_text", "select": {"tag": "text"}, "text": "all"})
    assert _elem(tool_ws, doc, "t1").text == "all"


@pytest.mark.parametrize(
    ("op", "match"),
    [
        ({"op": "replace_text", "ids": ["g1"], "text": "nope"}, "not a text element"),
        ({"op": "replace_text", "ids": ["t1"], "text": "bad\x00null"}, "control characters"),
        ({"op": "replace_text", "ids": ["does_not_exist"], "text": "x"}, "id 'does_not_exist'"),
    ],
)
def test_replace_text_rejections(tool_ws, doc, op, match) -> None:
    _rejected(tool_ws, doc, op, match=match)


# --- font ------------------------------------------------------------------------------


def test_font_writes_family_size_and_weight(tool_ws, doc) -> None:
    result = run_ops(
        doc, {"op": "font", "ids": ["t1"], "family": "Arial", "size": "12pt", "weight": "bold"}
    )
    style = _elem(tool_ws, doc, "t1").get("style")
    assert "font-family:Arial" in style
    assert "font-size:12pt" in style
    assert "font-weight:bold" in style
    assert result.changed is True and result.operation_id and result.snapshot_id


def test_font_accepts_a_selector(tool_ws, doc) -> None:
    run_ops(doc, {"op": "font", "select": {"tag": "text"}, "size": "24"})
    assert "font-size:24" in _elem(tool_ws, doc, "t1").get("style")


def test_font_size_accepts_a_number(tool_ws, doc) -> None:
    run_ops(doc, {"op": "font", "ids": ["t1"], "size": 8})
    assert "font-size:8" in _elem(tool_ws, doc, "t1").get("style")


@pytest.mark.parametrize(
    ("op", "match"),
    [
        ({"op": "font", "ids": ["t1"], "family": "Arial;fill:red{"}, "font"),
        ({"op": "font", "ids": ["t1"]}, "at least one of family / size / weight"),
        ({"op": "font", "ids": [], "family": "Arial"}, "'ids' is empty"),
    ],
)
def test_font_rejections(tool_ws, doc, op, match) -> None:
    _rejected(tool_ws, doc, op, match=match)


@pytest.fixture
def jp_doc(tool_ws: Path) -> str:
    return open_doc(tool_ws, "jp.svg", JP_SVG.encode())


def test_font_warns_about_a_non_covering_family_but_still_applies(
    tool_ws, jp_doc, monkeypatch
) -> None:
    monkeypatch.setattr(text_object, "uncovered_chars", lambda family, text: JP)
    monkeypatch.setattr(text_object, "suggest_covering_family", lambda chars: "Noto Sans CJK JP")
    result = run_ops(jp_doc, {"op": "font", "ids": ["jp"], "family": "Liberation Sans"})
    assert result.changed is True
    assert "font-family:Liberation Sans" in _elem(tool_ws, jp_doc, "jp").get("style")
    (warning,) = result.warnings
    assert warning.startswith("ops[0] font: 'Liberation Sans' cannot render")
    assert JP in warning and "'jp'" in warning and "try 'Noto Sans CJK JP'" in warning


def test_font_without_a_covering_suggestion_omits_the_hint(tool_ws, jp_doc, monkeypatch) -> None:
    monkeypatch.setattr(text_object, "uncovered_chars", lambda family, text: "こ")
    monkeypatch.setattr(text_object, "suggest_covering_family", lambda chars: None)
    (warning,) = run_ops(jp_doc, {"op": "font", "ids": ["jp"], "family": "Foo"}).warnings
    assert "try" not in warning


def test_font_covering_family_is_clean(tool_ws, jp_doc, monkeypatch) -> None:
    monkeypatch.setattr(text_object, "uncovered_chars", lambda family, text: "")
    assert (
        run_ops(jp_doc, {"op": "font", "ids": ["jp"], "family": "Noto Sans CJK JP"}).warnings == []
    )


def test_font_size_only_checks_no_coverage(tool_ws, jp_doc, monkeypatch) -> None:
    def boom(*_a: object) -> str:
        raise AssertionError("coverage must only be checked when the family changes")

    monkeypatch.setattr(text_object, "uncovered_chars", boom)
    assert run_ops(jp_doc, {"op": "font", "ids": ["jp"], "size": "20"}).warnings == []


def test_host_font_flags_japanese_in_a_latin_family(tool_ws, jp_doc) -> None:
    if not _installed("Liberation Sans"):
        pytest.skip("Liberation Sans not installed on host")
    if suggest_covering_family(JP) is None:
        pytest.skip("no CJK-covering font installed on host to suggest")
    (warning,) = run_ops(
        jp_doc, {"op": "font", "ids": ["jp"], "family": "Liberation Sans"}
    ).warnings
    assert JP in warning and "try" in warning


def test_host_font_covering_family_is_clean(tool_ws, jp_doc) -> None:
    if not _installed("Noto Sans CJK JP"):
        pytest.skip("Noto Sans CJK JP not installed on host")
    assert (
        run_ops(jp_doc, {"op": "font", "ids": ["jp"], "family": "Noto Sans CJK JP"}).warnings == []
    )


def test_host_font_latin_text_is_no_false_positive(tool_ws, doc) -> None:
    if not _installed("Liberation Sans"):
        pytest.skip("Liberation Sans not installed on host")
    # spaces and punctuation must not count as uncovered
    result = run_ops(
        doc,
        {"op": "replace_text", "ids": ["t1"], "text": "Hello, World! (123)"},
        {"op": "font", "ids": ["t1"], "family": "Liberation Sans"},
    )
    assert result.warnings == []
