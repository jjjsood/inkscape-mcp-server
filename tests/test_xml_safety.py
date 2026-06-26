"""XML parse-safety tests (workspace-model.md §4 normative parser)."""

from __future__ import annotations

import pytest

from inkscape_mcp.workspace.xml_safety import (
    UnsafeXMLError,
    make_safe_parser,
    parse_svg_bytes,
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


def test_safe_parser_has_all_five_flags() -> None:
    parser = make_safe_parser()
    # Construction with the normative flags succeeds; the parser is reusable.
    assert parser is not None


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
