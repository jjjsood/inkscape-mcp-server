"""Direct-DOM style edit engine (ADR-005).

Pure ``fn(tree, ...) -> str`` edits for the five style tools (``set_fill``, ``set_stroke``,
``set_opacity``, ``replace_color``, ``apply_palette``). Each edits the parsed working tree IN MEMORY
(called from ``edit.ops`` inside the shared edit pipeline) and returns a short human summary of
what changed. No MCP decorators live here — the ``edit`` tool (``inkscape_mcp.tools.edit``)
wraps these and maps exceptions to ``ToolError``.

SAFETY (sec.12): every colour input is validated through :func:`normalize_color` and every
length through :func:`normalize_length` (both reject CSS-injection punctuation) BEFORE it is
written into an attribute or inline ``style`` block. Opacity values are bounded to ``[0, 1]``.
The only client inputs that reach a DOM mutation are object ids (used solely for in-tree lookups)
and these validated style parameters. The original source file is never touched — all writes land
on the working copy via the pipeline.
"""

from __future__ import annotations

import re

from lxml import etree

from inkscape_mcp.document.inspect import (
    _CSS_RULE_RE,
    _CSS_SIMPLE_SELECTOR_RE,
    _CssRule,
    _document_css_rules,
    _element_classes,
    _own_paint_value,
    is_element,
    local_name,
)
from inkscape_mcp.edit.dom import (
    EditError,
    color_key,
    fmt_num,
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

#: CSS named colours (Color Module Level 4) plus the SVG keywords ``none`` / ``transparent`` /
#: ``currentcolor``. Used to STRICTLY validate a named-colour token: :func:`normalize_color`
#: accepts any letters-only word as a "named colour", which is too permissive for ``apply_palette``
#: (a typo like ``notacolor`` would slip through as a silent no-op). A palette colour name
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
    gap where a typo such as ``notacolor`` was silently accepted as a "named colour".
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


def _plural(count: int) -> str:
    return "object" if count == 1 else "objects"


def set_fill(
    tree: etree._ElementTree, object_ids: list[str], color: str, opacity: float | None = None
) -> str:
    """Set ``fill`` (and optionally ``fill-opacity``) on each target.

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
    targets = require_targets(tree.getroot(), object_ids)
    for elem in targets:
        set_style_property(elem, "fill", fill, key=fill_key)
        if fill_opacity is not None:
            set_style_property(elem, "fill-opacity", fmt_num(fill_opacity))
    count = len(targets)
    return f"set fill on {count} {_plural(count)}"


def set_stroke(
    tree: etree._ElementTree,
    object_ids: list[str],
    color: str | None = None,
    width: str | None = None,
    opacity: float | None = None,
) -> str:
    """Set any provided of ``stroke`` / ``stroke-width`` / ``stroke-opacity``.

    At least one of ``color``, ``width``, ``opacity`` must be supplied (else :class:`EditError`).
    Each is validated eagerly: paint via :func:`normalize_paint` (a colour OR a ``url(#id)``
    paint-server reference), width via :func:`normalize_length`, opacity bounded to ``[0, 1]``.
    """
    if color is None and width is None and opacity is None:
        raise EditError("set_stroke requires at least one of color, width, opacity")

    stroke = None if color is None else normalize_paint(color)
    # See set_fill: a url(#id) ref compares verbatim, a colour uses the color_key no-op key.
    stroke_key = None if (stroke is not None and is_url_paint(stroke)) else color_key
    stroke_width = None if width is None else normalize_length(width)
    stroke_opacity = None if opacity is None else _check_opacity(opacity)
    targets = require_targets(tree.getroot(), object_ids)
    changed: list[str] = []
    for elem in targets:
        if stroke is not None:
            set_style_property(elem, "stroke", stroke, key=stroke_key)
        if stroke_width is not None:
            set_style_property(elem, "stroke-width", stroke_width)
        if stroke_opacity is not None:
            set_style_property(elem, "stroke-opacity", fmt_num(stroke_opacity))
    if stroke is not None:
        changed.append("color")
    if stroke_width is not None:
        changed.append("width")
    if stroke_opacity is not None:
        changed.append("opacity")
    count = len(targets)
    return f"set stroke {'/'.join(changed)} on {count} {_plural(count)}"


def set_opacity(tree: etree._ElementTree, object_ids: list[str], opacity: float) -> str:
    """Set element-level ``opacity`` (``[0, 1]``) on each target."""
    formatted = fmt_num(_check_opacity(opacity))
    targets = require_targets(tree.getroot(), object_ids)
    for elem in targets:
        set_style_property(elem, "opacity", formatted)
    count = len(targets)
    return f"set opacity on {count} {_plural(count)}"


def _scope_elements(root: etree._Element, scope_ids: list[str] | None) -> list[etree._Element]:
    """Return the elements (and all their descendants) to scan for colour replacement.

    With ``scope_ids`` given, replacement is confined to those elements' subtrees (each id must
    exist, else :class:`TargetNotFound` via :func:`require_targets`). Without a scope, the whole
    document (every element under the root, root included) is scanned.
    """
    if scope_ids is None:
        return [elem for elem in root.iter() if is_element(elem)]
    roots = require_targets(root, scope_ids)
    elements: list[etree._Element] = []
    seen: set[int] = set()
    for sub in roots:
        for elem in sub.iter():
            if is_element(elem) and id(elem) not in seen:
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


#: One colour declaration inside a `<style>` body: `fill: #e63946` (group 1 = prefix, 2 = value).
#: Possessive quantifiers only: the value is taken in one pass, so the scan is linear on any input
#: (a lazy value + lookahead backtracked cubically on whitespace — a ReDoS on untrusted CSS).
_CSS_COLOR_DECL_RE = re.compile(
    r"((?<![\w-])(?:" + "|".join(_COLOR_PROPERTIES) + r")\s*+:\s*+)([^;{}!]*+)"
)

#: `<style>` text larger than this is left alone (bounds the work on untrusted documents).
_MAX_CSS_CHARS = 1 << 20


def _rewrite_css(text: str, from_key: str, to_color: str) -> tuple[str, int]:
    count = 0

    def sub(match: re.Match[str]) -> str:
        nonlocal count
        raw = match.group(2)
        value = raw.rstrip()
        # `fill:hover {` style selectors are not declarations.
        if match.string[match.end() : match.end() + 1] == "{" or color_key(value) != from_key:
            return match.group(0)
        count += 1
        return match.group(1) + to_color + raw[len(value) :]

    return _CSS_COLOR_DECL_RE.sub(sub, text), count


def _replace_color_in_css(elem: etree._Element, from_key: str, to_color: str) -> int:
    """Rewrite matching colour declarations in one `<style>` element (its text and child tails)."""
    total = 0
    if elem.text and len(elem.text) <= _MAX_CSS_CHARS:
        elem.text, n = _rewrite_css(elem.text, from_key, to_color)
        total += n
    for child in elem:
        if child.tail and len(child.tail) <= _MAX_CSS_CHARS:
            child.tail, n = _rewrite_css(child.tail, from_key, to_color)
            total += n
    return total


_CssKey = tuple[str | None, str | None, str | None]


def _rule_key(rule: _CssRule) -> _CssKey:
    return (rule.tag, rule.cls, rule.elem_id)


def _winning_rule(elem: etree._Element, rules: list[_CssRule], prop: str) -> _CssRule | None:
    """The `<style>` rule that supplies `prop` to `elem` (same cascade as `_own_paint_value`)."""
    classes, tag, elem_id = _element_classes(elem), local_name(elem), elem.get("id")
    best: _CssRule | None = None
    for rule in rules:
        if prop in rule.decls and rule.matches(tag=tag, classes=classes, elem_id=elem_id):
            if best is None or (rule.specificity, rule.order) > (best.specificity, best.order):
                best = rule
    return best


def _rewritable_rule_keys(root: etree._Element, from_key: str, in_scope: set[int]) -> set[_CssKey]:
    """Class/id rule keys whose `from_key` colour may be rewritten IN the `<style>` rule.

    A rule qualifies when it is the winning source of a `from_key` paint (no inline/attribute
    override) for at least one in-scope element and for NO out-of-scope element: a class shared
    with untargeted elements must not recolour them, so those keep the inline-override fallback.
    """
    rules = _document_css_rules(root)
    used: set[_CssKey] = set()
    blocked: set[_CssKey] = set()
    for elem in root.iter():
        if not is_element(elem):
            continue
        for prop in _COLOR_PROPERTIES:
            if prop in parse_style(elem) or elem.get(prop) is not None:
                continue
            rule = _winning_rule(elem, rules, prop)
            if rule is None or color_key(rule.decls[prop].strip()) != from_key:
                continue
            key = _rule_key(rule)
            if rule.cls is None and rule.elem_id is None:
                blocked.add(key)  # type / universal selectors are never rewritten
            (used if id(elem) in in_scope else blocked).add(key)
    return used - blocked


def _rewrite_css_rules(
    text: str, keys: set[_CssKey], from_key: str, to_color: str
) -> tuple[str, int]:
    """Rewrite `from_key` colours inside rule blocks whose selectors are ALL in `keys`."""
    total = 0

    def block(match: re.Match[str]) -> str:
        nonlocal total
        sels = [s.split()[-1] for s in match.group(1).split(",") if s.strip()]
        found = [_CSS_SIMPLE_SELECTOR_RE.match(t) for t in sels]
        if not found or any(
            m is None or (m.group(1), m.group(2), m.group(3)) not in keys for m in found
        ):
            return match.group(0)
        body, n = _rewrite_css(match.group(2), from_key, to_color)
        total += n
        return match.group(1) + "{" + body + "}"

    return _CSS_RULE_RE.sub(block, text), total


def _replace_color_in_tree(
    root: etree._Element,
    from_color: str,
    to_color: str,
    scope_ids: list[str] | None,
) -> int:
    """Replace ``from_color`` with ``to_color`` across the document (or within ``scope_ids``).

    Both colours are assumed already validated by the caller. Returns the total number of
    property/attribute rewrites made. A scope naming the root ``<svg>`` is document-wide; a
    ``<style>`` element in the scope has all its colour declarations rewritten.
    """
    from_key = color_key(from_color)
    styles: list[etree._Element] = []
    if scope_ids is not None:
        targets = require_targets(root, scope_ids)
        if any(t is root for t in targets):
            scope_ids = None
        else:
            styles = [t for t in targets if local_name(t) == "style"]
    total = 0
    scope = _scope_elements(root, scope_ids)
    for elem in scope:
        total += _replace_color_in_element(elem, from_key, to_color)
        if scope_ids is None and local_name(elem) == "style":
            total += _replace_color_in_css(elem, from_key, to_color)
    for style in styles:
        total += _replace_color_in_css(style, from_key, to_color)
    if scope_ids is None:
        return total
    # Scoped: a CSS-painted target (class / id rule) is recoloured IN its rule when only targeted
    # elements use that rule; otherwise (shared class, type selector) overridden inline per element.
    keys = _rewritable_rule_keys(root, from_key, {id(e) for e in scope})
    if keys:
        for style in root.iter():
            if is_element(style) and local_name(style) == "style":
                for node, attr in [(style, "text"), *((c, "tail") for c in style)]:
                    text = getattr(node, attr)
                    if text and len(text) <= _MAX_CSS_CHARS:
                        new, n = _rewrite_css_rules(text, keys, from_key, to_color)
                        setattr(node, attr, new)
                        total += n
    rules = _document_css_rules(root)
    for elem in scope if rules else ():
        for prop in _COLOR_PROPERTIES:
            value = _own_paint_value(elem, rules, prop)
            if value is not None and color_key(value) == from_key:
                set_style_property(elem, prop, to_color)
                total += 1
    return total


def replace_color(
    tree: etree._ElementTree, from_color: str, to_color: str, scope_ids: list[str] | None = None
) -> str:
    """Replace one colour with another across the document or a scope.

    Both colours are validated/canonicalized eagerly. Matching is by :func:`color_key` (casefold
    + hex shorthand expansion) against inline-style colour properties AND presentation attributes;
    the normalized ``to_color`` is written. The summary reports how many replacements were made.
    """
    normalized_from = normalize_color(from_color)
    normalized_to = normalize_color(to_color)
    count = _replace_color_in_tree(tree.getroot(), normalized_from, normalized_to, scope_ids)
    return f"replaced {normalized_from} with {normalized_to} ({count} {_plural(count)})"


def apply_palette(
    tree: etree._ElementTree, mapping: dict[str, str], scope_ids: list[str] | None = None
) -> str:
    """Apply many ``from -> to`` colour replacements in one operation.

    Every key AND value is STRICTLY colour-validated eagerly via :func:`validate_palette_color`
    (a typo'd named colour such as ``notacolor`` is rejected, not silently accepted), so
    an invalid entry raises BEFORE the tree is touched and therefore before any op record, snapshot,
    or write exists. Each mapping entry reuses the same colour-matching logic as
    :func:`replace_color`. The summary reports the total replacement count across entries.
    """
    if not mapping:
        raise EditError("apply_palette requires a non-empty colour mapping")
    normalized: list[tuple[str, str]] = [
        (validate_palette_color(src), validate_palette_color(dst)) for src, dst in mapping.items()
    ]
    root = tree.getroot()
    total = 0
    for src, dst in normalized:
        total += _replace_color_in_tree(root, src, dst, scope_ids)
    return f"applied palette ({len(normalized)} mappings, {total} {_plural(total)})"


__all__ = [
    "apply_palette",
    "replace_color",
    "set_fill",
    "set_opacity",
    "set_stroke",
    "validate_palette_color",
]
