"""XML parse-safety tests (workspace-model.md §4 normative parser)."""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from inkscape_mcp.config import Settings
from inkscape_mcp.workspace import xml_safety
from inkscape_mcp.workspace.limits import LimitExceeded
from inkscape_mcp.workspace.xml_safety import (
    UnsafeXMLError,
    make_safe_parser,
    parse_svg_bytes,
    parse_svg_file,
    parse_svg_string,
)

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<svg xmlns="http://www.w3.org/2000/svg"><text>&lol3;</text></svg>
"""

XXE_EXTERNAL = b"""<?xml version="1.0"?>
<!DOCTYPE svg [
 <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<svg xmlns="http://www.w3.org/2000/svg"><text>&xxe;</text></svg>
"""

WELL_FORMED = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'


def test_safe_parser_has_all_five_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    # lxml does not expose parser options, so capture the constructor kwargs.
    captured: dict[str, object] = {}
    real = etree.XMLParser

    def spy(**kwargs: object) -> etree.XMLParser:
        captured.update(kwargs)
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(etree, "XMLParser", spy)
    make_safe_parser()
    assert captured == {
        "resolve_entities": False,
        "no_network": True,
        "load_dtd": False,
        "dtd_validation": False,
        "huge_tree": False,
    }


def test_huge_tree_off_keeps_depth_guard() -> None:
    # huge_tree=False -> libxml2's 256-level depth guard stays on.
    deep = b'<svg xmlns="http://www.w3.org/2000/svg">' + b"<g>" * 300 + b"</g>" * 300 + b"</svg>"
    with pytest.raises(UnsafeXMLError):
        parse_svg_bytes(deep)


def test_billion_laughs_does_not_expand() -> None:
    # resolve_entities=False -> the &lol3; reference is left unexpanded (no memory blowup).
    tree = parse_svg_bytes(BILLION_LAUGHS)
    text = tree.getroot().findtext("{http://www.w3.org/2000/svg}text")
    # The entity is NOT expanded into thousands of "lol"; the body is empty / unexpanded.
    assert text in (None, "", "&lol3;")
    assert "lollollol" not in (text or "")


def test_external_dtd_xxe_does_not_fetch() -> None:
    # load_dtd=False + no_network=True + resolve_entities=False -> /etc/passwd never read.
    tree = parse_svg_bytes(XXE_EXTERNAL)
    text = tree.getroot().findtext("{http://www.w3.org/2000/svg}text")
    assert text in (None, "", "&xxe;")
    assert "root:" not in (text or ""), "XXE expanded /etc/passwd contents"


def test_well_formed_svg_parses() -> None:
    tree = parse_svg_bytes(WELL_FORMED)
    root = tree.getroot()
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.get("width") == "10"


def test_malformed_raises_unsafe_xml_error() -> None:
    with pytest.raises(UnsafeXMLError):
        parse_svg_bytes(b"<svg><not-closed>")


def _parser_must_not_run(_data: bytes) -> etree._ElementTree:
    raise AssertionError("parser called before the size check")


def test_oversized_string_rejected_before_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xml_safety, "parse_svg_bytes", _parser_must_not_run)
    with pytest.raises(LimitExceeded):
        parse_svg_string(WELL_FORMED.decode() + " " * 100, Settings(max_input_bytes=10))


def test_oversized_file_rejected_before_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "big.svg"
    f.write_bytes(WELL_FORMED + b" " * 100)
    monkeypatch.setattr(xml_safety, "parse_svg_bytes", _parser_must_not_run)
    with pytest.raises(LimitExceeded):
        parse_svg_file(f, Settings(max_input_bytes=10))


@pytest.mark.parametrize(
    "doctype",
    [
        # external DTD via a SYSTEM id
        b'<!DOCTYPE svg SYSTEM "file:///etc/passwd">',
        # parameter entity expanded in the internal subset, no entity ref in the body
        b'<!DOCTYPE svg [<!ENTITY % x SYSTEM "file:///etc/passwd"> %x;]>',
    ],
)
def test_external_dtd_and_parameter_entity_not_loaded(doctype: bytes) -> None:
    data = b'<?xml version="1.0"?>' + doctype + b'<svg xmlns="http://www.w3.org/2000/svg"/>'
    try:
        tree = parse_svg_bytes(data)
    except UnsafeXMLError as exc:  # refusal is equally safe
        assert "root:" not in str(exc)
        return
    assert b"root:" not in etree.tostring(tree)


def test_lone_surrogate_string_is_unsafe_xml_error() -> None:
    with pytest.raises(UnsafeXMLError):
        parse_svg_string("<svg>\ud800</svg>")


def test_file_grown_after_stat_is_still_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "grown.svg"
    f.write_bytes(WELL_FORMED + b" " * 100)
    monkeypatch.setattr(xml_safety, "check_input_size", lambda *_a: 0)  # stat saw a small file
    monkeypatch.setattr(xml_safety, "parse_svg_bytes", _parser_must_not_run)
    with pytest.raises(LimitExceeded):
        parse_svg_file(f, Settings(max_input_bytes=10))
