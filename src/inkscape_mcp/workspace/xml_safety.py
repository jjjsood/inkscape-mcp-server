"""Safe XML parsing (workspace model — "XML parse safety").

The only protection against billion-laughs / XXE / external-DTD fetch is the parser
configuration. `make_safe_parser()` returns the NORMATIVE parser; all five flags are
required and a missing one is a defect. Untrusted SVG input must only ever be parsed
through these helpers.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from inkscape_mcp.config import Settings
from inkscape_mcp.workspace.limits import check_input_size


class UnsafeXMLError(Exception):
    """SVG/XML input failed to parse safely (malformed, or blocked by the safe parser)."""


def make_safe_parser() -> etree.XMLParser:
    """Return the normative lxml parser for all untrusted SVG input (§4).

    All five flags are required:
    - resolve_entities=False  (billion-laughs: do not expand entities)
    - no_network=True         (never fetch external resources)
    - load_dtd=False          (block external-DTD side channels)
    - dtd_validation=False
    - huge_tree=False         (keep lxml's node/depth guards on)
    """
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
    )


def parse_svg_bytes(data: bytes) -> etree._ElementTree:
    """Parse SVG/XML bytes with the safe parser; raise `UnsafeXMLError` on failure.

    The error message is stable and carries no host path.
    """
    parser = make_safe_parser()
    try:
        root = etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise UnsafeXMLError(f"failed to parse SVG: {exc}") from exc
    if root is None:
        raise UnsafeXMLError("failed to parse SVG: empty document")
    return etree.ElementTree(root)


def parse_svg_string(text: str) -> etree._ElementTree:
    """Parse an SVG/XML STRING with the safe parser; raise `UnsafeXMLError` on failure.

    The string counterpart of :func:`parse_svg_bytes` for agent-composed SVG that arrives as a
    Python `str` (`set_document_svg` / `insert_svg_fragment`). It encodes to UTF-8 and runs
    the SAME normative hardened parser (XXE off, no network, no entity expansion, huge_tree off), so
    no string variant can ever bypass the §4 safety configuration. A leading XML declaration with an
    explicit encoding is tolerated (lxml rejects an encoding declaration on a `str`, so the encode
    is mandatory). The error message is stable and carries no host path.
    """
    return parse_svg_bytes(text.encode("utf-8"))


def parse_svg_file(path: Path, settings: Settings | None = None) -> etree._ElementTree:
    """Check input size, read raw bytes, then parse with the safe parser.

    Size is checked before any read/parse (§4). Raises `UnsafeXMLError` on parse failure;
    `LimitExceeded` if the file is over the input cap.
    """
    check_input_size(path, settings)
    data = path.read_bytes()
    return parse_svg_bytes(data)
