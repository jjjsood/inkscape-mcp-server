"""Direct-DOM style edit engine (E2-01, ADR-005).

Pure ``mutate(tree) -> str`` builders for the five style tools (``set_fill``, ``set_stroke``,
``set_opacity``, ``replace_color``, ``apply_palette``). Each function returns a closure suitable
as the ``mutate`` callback of :func:`inkscape_mcp.edit.pipeline.apply_edit`: it edits the parsed
working tree IN MEMORY and returns a short human summary of what changed. No MCP decorators live
here — the ``inkscape_mcp.tools.style`` layer wraps these and maps exceptions to ``ToolError``.

SAFETY (sec.12): every colour input is validated through :func:`normalize_color` and every
length through :func:`normalize_length` (both reject CSS-injection punctuation) BEFORE it is
written into an attribute or inline ``style`` block. Opacity values are bounded to ``[0, 1]``.
The only client inputs that reach a DOM mutation are object ids (used solely for in-tree lookups)
and these validated style parameters. The original source file is never touched — all writes land
on the working copy via the pipeline.
"""

from __future__ import annotations

from collections.abc import Callable

from lxml import etree

from inkscape_mcp.edit.dom import (
    EditError,
    color_key,
    is_url_paint,
    normalize_color,
    normalize_length,
    normalize_paint,
    parse_style,
    require_targets,
    set_style_property,
)

#: Inline-style colour properties scanned/rewritten by colour-replacement edits. These are the
#: CSS properties whose value is a colour; each is matched against the requested ``from`` colour
#: via :func:`color_key` and rewritten in place. The same names double as presentation attributes
#: (e.g. ``fill="red"``), which are handled identically — see :func:`_replace_color_in_tree`.
_COLOR_PROPERTIES: tuple[str, ...] = (
    "fill",
    "stroke",
    "stop-color",
    "flood-color",
    "lighting-color",
    "color",
)

#: The ``mutate`` callback type re-declared locally to avoid importing from the pipeline (which
#: would create an import cycle through the tools layer). Matches
#: :data:`inkscape_mcp.edit.pipeline.MutateFn`.
MutateFn = Callable[[etree._ElementTree], str]

#: CSS named colours (Color Module Level 4) plus the SVG keywords ``none`` / ``transparent`` /
#: ``currentcolor``. Used to STRICTLY validate a named-colour token: :func:`normalize_color`
#: accepts any letters-only word as a "named colour", which is too permissive for ``apply_palette``
#: (a typo like ``notacolor`` would slip through as a silent no-op — E10-02). A palette colour name
#: must be one of these to be accepted. Stored casefolded; lookups casefold the candidate.
_CSS_NAMED_COLORS: frozenset[str] = frozenset(
    {
        "none",
        "transparent",
        "currentcolor",
        "aliceblue",
        "antiquewhite",
        "aqua",
        "aquamarine",
        "azure",
        "beige",
        "bisque",
        "black",
        "blanchedalmond",
        "blue",
        "blueviolet",
        "brown",
        "burlywood",
        "cadetblue",
        "chartreuse",
        "chocolate",
        "coral",
        "cornflowerblue",
        "cornsilk",
        "crimson",
        "cyan",
        "darkblue",
        "darkcyan",
        "darkgoldenrod",
        "darkgray",
        "darkgreen",
        "darkgrey",
        "darkkhaki",
        "darkmagenta",
        "darkolivegreen",
        "darkorange",
        "darkorchid",
        "darkred",
        "darksalmon",
        "darkseagreen",
        "darkslateblue",
        "darkslategray",
        "darkslategrey",
        "darkturquoise",
        "darkviolet",
        "deeppink",
        "deepskyblue",
        "dimgray",
        "dimgrey",
        "dodgerblue",
        "firebrick",
        "floralwhite",
        "forestgreen",
        "fuchsia",
        "gainsboro",
        "ghostwhite",
        "gold",
        "goldenrod",
        "gray",
        "green",
        "greenyellow",
        "grey",
        "honeydew",
        "hotpink",
        "indianred",
        "indigo",
        "ivory",
        "khaki",
        "lavender",
        "lavenderblush",
        "lawngreen",
        "lemonchiffon",
        "lightblue",
        "lightcoral",
        "lightcyan",
        "lightgoldenrodyellow",
        "lightgray",
        "lightgreen",
        "lightgrey",
        "lightpink",
        "lightsalmon",
        "lightseagreen",
        "lightskyblue",
        "lightslategray",
        "lightslategrey",
        "lightsteelblue",
        "lightyellow",
        "lime",
        "limegreen",
        "linen",
        "magenta",
        "maroon",
        "mediumaquamarine",
        "mediumblue",
        "mediumorchid",
        "mediumpurple",
        "mediumseagreen",
        "mediumslateblue",
        "mediumspringgreen",
        "mediumturquoise",
        "mediumvioletred",
        "midnightblue",
        "mintcream",
        "mistyrose",
        "moccasin",
        "navajowhite",
        "navy",
        "oldlace",
        "olive",
        "olivedrab",
        "orange",
        "orangered",
        "orchid",
        "palegoldenrod",
        "palegreen",
        "paleturquoise",
        "palevioletred",
        "papayawhip",
        "peachpuff",
        "peru",
        "pink",
        "plum",
        "powderblue",
        "purple",
        "rebeccapurple",
        "red",
        "rosybrown",
        "royalblue",
        "saddlebrown",
        "salmon",
        "sandybrown",
        "seagreen",
        "seashell",
        "sienna",
        "silver",
        "skyblue",
        "slateblue",
        "slategray",
        "slategrey",
        "snow",
        "springgreen",
        "steelblue",
        "tan",
        "teal",
        "thistle",
        "tomato",
        "turquoise",
        "violet",
        "wheat",
        "white",
        "whitesmoke",
        "yellow",
        "yellowgreen",
    }
)


def validate_palette_color(raw: str) -> str:
    """Validate a palette colour STRICTLY, or raise :class:`EditError`.

    Stricter than :func:`normalize_color`: a named colour must be a real CSS colour keyword (in
    :data:`_CSS_NAMED_COLORS`), not merely a letters-only word. Hex and ``rgb()/hsl()`` forms are
    accepted exactly as :func:`normalize_color` accepts them. This closes the ``apply_palette``
    gap where a typo such as ``notacolor`` was silently accepted as a "named colour" (E10-02).
    Returns the canonicalized colour string.
    """
    canon = normalize_color(raw)
    # normalize_color lowercases names and hex; a func form has no letters-only shape. A name
    # candidate is a pure-alpha token; reject it unless it is a recognised CSS colour keyword.
    if canon.isalpha() and canon not in _CSS_NAMED_COLORS:
        raise EditError(f"invalid colour value: {raw!r}")
    return canon


def _check_opacity(value: float) -> float:
    """Validate an opacity value is a finite number in ``[0, 1]`` or raise :class:`EditError`."""
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise EditError("opacity must be a number between 0 and 1") from exc
    if not (0.0 <= num <= 1.0):
        raise EditError("opacity must be between 0 and 1")
    return num


def _fmt_opacity(value: float) -> str:
    """Format an opacity value compactly (trailing zeros trimmed) for an attribute/style value."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _plural(count: int) -> str:
    return "object" if count == 1 else "objects"


def set_fill_mutate(object_ids: list[str], color: str, opacity: float | None = None) -> MutateFn:
    """Build a mutate that sets ``fill`` (and optionally ``fill-opacity``) on each target.

    The paint is validated/canonicalized eagerly so an invalid value is rejected before any
    pipeline side effect. ``color`` accepts a colour OR a ``url(#id)`` paint-server reference (a
    gradient/pattern in ``<defs>``, e.g. an id from ``add_linear_gradient``) via
    :func:`normalize_paint`. ``opacity`` (when given) is bounded to ``[0, 1]``.
    """
    fill = normalize_paint(color)
    # A colour uses the casefold/hex-expand no-op key; a url(#id) ref is case-sensitive, so it is
    # compared verbatim (key=None) — lowercasing an id could falsely treat two distinct refs as one.
    fill_key = None if is_url_paint(fill) else color_key
    fill_opacity = None if opacity is None else _check_opacity(opacity)

    def mutate(tree: etree._ElementTree) -> str:
        targets = require_targets(tree.getroot(), object_ids)
        for elem in targets:
            set_style_property(elem, "fill", fill, key=fill_key)
            if fill_opacity is not None:
                set_style_property(elem, "fill-opacity", _fmt_opacity(fill_opacity))
        count = len(targets)
        return f"set fill on {count} {_plural(count)}"

    return mutate


def set_stroke_mutate(
    object_ids: list[str],
    color: str | None = None,
    width: str | None = None,
    opacity: float | None = None,
) -> MutateFn:
    """Build a mutate that sets any provided of ``stroke`` / ``stroke-width`` / ``stroke-opacity``.

    At least one of ``color``, ``width``, ``opacity`` must be supplied (else :class:`EditError`).
    Each is validated eagerly: paint via :func:`normalize_paint` (a colour OR a ``url(#id)``
    paint-server reference), width via :func:`normalize_length`, opacity bounded to ``[0, 1]``.
    """
    if color is None and width is None and opacity is None:
        raise EditError("set_stroke requires at least one of color, width, opacity")

    stroke = None if color is None else normalize_paint(color)
    # See set_fill_mutate: a url(#id) ref compares verbatim, a colour uses the color_key no-op key.
    stroke_key = None if (stroke is not None and is_url_paint(stroke)) else color_key
    stroke_width = None if width is None else normalize_length(width)
    stroke_opacity = None if opacity is None else _check_opacity(opacity)

    def mutate(tree: etree._ElementTree) -> str:
        targets = require_targets(tree.getroot(), object_ids)
        changed: list[str] = []
        for elem in targets:
            if stroke is not None:
                set_style_property(elem, "stroke", stroke, key=stroke_key)
            if stroke_width is not None:
                set_style_property(elem, "stroke-width", stroke_width)
            if stroke_opacity is not None:
                set_style_property(elem, "stroke-opacity", _fmt_opacity(stroke_opacity))
        if stroke is not None:
            changed.append("color")
        if stroke_width is not None:
            changed.append("width")
        if stroke_opacity is not None:
            changed.append("opacity")
        count = len(targets)
        return f"set stroke {'/'.join(changed)} on {count} {_plural(count)}"

    return mutate


def set_opacity_mutate(object_ids: list[str], opacity: float) -> MutateFn:
    """Build a mutate that sets element-level ``opacity`` (``[0, 1]``) on each target."""
    value = _check_opacity(opacity)
    formatted = _fmt_opacity(value)

    def mutate(tree: etree._ElementTree) -> str:
        targets = require_targets(tree.getroot(), object_ids)
        for elem in targets:
            set_style_property(elem, "opacity", formatted)
        count = len(targets)
        return f"set opacity on {count} {_plural(count)}"

    return mutate


def _is_element(elem: etree._Element) -> bool:
    return isinstance(elem.tag, str)


def _scope_elements(root: etree._Element, scope_ids: list[str] | None) -> list[etree._Element]:
    """Return the elements (and all their descendants) to scan for colour replacement.

    With ``scope_ids`` given, replacement is confined to those elements' subtrees (each id must
    exist, else :class:`TargetNotFound` via :func:`require_targets`). Without a scope, the whole
    document (every element under the root, root included) is scanned.
    """
    if scope_ids is None:
        return [elem for elem in root.iter() if _is_element(elem)]
    roots = require_targets(root, scope_ids)
    elements: list[etree._Element] = []
    seen: set[int] = set()
    for sub in roots:
        for elem in sub.iter():
            if _is_element(elem) and id(elem) not in seen:
                seen.add(id(elem))
                elements.append(elem)
    return elements


def _replace_color_in_element(elem: etree._Element, from_key: str, to_color: str) -> int:
    """Rewrite every colour occurrence equal to ``from_key`` on one element to ``to_color``.

    Matches both inline-style colour declarations (``style="fill:..."``) and the same-named
    presentation attributes (``fill="..."``), comparing via the casefold/hex-expanded
    :func:`color_key`. Returns the number of individual property/attribute rewrites made.
    """
    replacements = 0

    # Inline-style colour properties. set_style_property drops any same-named presentation
    # attribute, so handle the style block first; the attribute pass below then sees a clean slate.
    decls = parse_style(elem)
    for prop in _COLOR_PROPERTIES:
        current = decls.get(prop)
        if current is not None and color_key(current) == from_key:
            set_style_property(elem, prop, to_color)
            replacements += 1

    # Presentation attributes (only those not already overridden by the style block above).
    for prop in _COLOR_PROPERTIES:
        if prop in decls:
            continue
        current = elem.get(prop)
        if current is not None and color_key(current) == from_key:
            elem.set(prop, to_color)
            replacements += 1

    return replacements


def _replace_color_in_tree(
    root: etree._Element,
    from_color: str,
    to_color: str,
    scope_ids: list[str] | None,
) -> int:
    """Replace ``from_color`` with ``to_color`` across the document (or within ``scope_ids``).

    Both colours are assumed already validated by the caller. Returns the total number of
    property/attribute rewrites made.
    """
    from_key = color_key(from_color)
    total = 0
    for elem in _scope_elements(root, scope_ids):
        total += _replace_color_in_element(elem, from_key, to_color)
    return total


def replace_color_mutate(
    from_color: str, to_color: str, scope_ids: list[str] | None = None
) -> MutateFn:
    """Build a mutate that replaces one colour with another across the document or a scope.

    Both colours are validated/canonicalized eagerly. Matching is by :func:`color_key` (casefold
    + hex shorthand expansion) against inline-style colour properties AND presentation attributes;
    the normalized ``to_color`` is written. The summary reports how many replacements were made.
    """
    normalized_from = normalize_color(from_color)
    normalized_to = normalize_color(to_color)

    def mutate(tree: etree._ElementTree) -> str:
        count = _replace_color_in_tree(tree.getroot(), normalized_from, normalized_to, scope_ids)
        return f"replaced {normalized_from} with {normalized_to} ({count} {_plural(count)})"

    return mutate


def apply_palette_mutate(mapping: dict[str, str], scope_ids: list[str] | None = None) -> MutateFn:
    """Build a mutate that applies many ``from -> to`` colour replacements in one operation.

    Every key AND value is STRICTLY colour-validated eagerly via :func:`validate_palette_color`
    (a typo'd named colour such as ``notacolor`` is rejected, not silently accepted — E10-02), so
    an invalid entry raises BEFORE the builder returns and therefore before any op record, snapshot,
    or write exists. Each mapping entry reuses the same colour-matching logic as
    :func:`replace_color_mutate`. The summary reports the total replacement count across entries.
    """
    if not mapping:
        raise EditError("apply_palette requires a non-empty colour mapping")
    normalized: list[tuple[str, str]] = [
        (validate_palette_color(src), validate_palette_color(dst)) for src, dst in mapping.items()
    ]

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        total = 0
        for src, dst in normalized:
            total += _replace_color_in_tree(root, src, dst, scope_ids)
        return f"applied palette ({len(normalized)} mappings, {total} {_plural(total)})"

    return mutate


__all__ = [
    "MutateFn",
    "apply_palette_mutate",
    "replace_color_mutate",
    "set_fill_mutate",
    "set_opacity_mutate",
    "set_stroke_mutate",
    "validate_palette_color",
]
