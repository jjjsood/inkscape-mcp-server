"""Direct-DOM editing primitives (ADR-005).

Pure lxml helpers shared by the style / text / transform engines. They parse the WORKING
COPY through the normative safe parser, resolve targets by id, edit in memory, and serialize
back to the working copy only — the original source file is NEVER touched here.

SAFETY (sec.12): values that land in an attribute or an inline `style="…"` block are validated
before they are written. Colour, font, weight, and numeric inputs are pattern-checked so a
caller cannot inject extra CSS declarations (`;`, `{`, `}`) or non-finite/garbage tokens. The
only client inputs that reach this module are a `doc_id`, object ids (used solely for in-tree
lookups, never argv), and the typed edit parameters validated below.
"""

from __future__ import annotations

import copy
import math
import os
import re
import secrets
from collections.abc import Callable
from pathlib import Path

from lxml import etree

from inkscape_mcp.document.inspect import (
    INKSCAPE_NS,
    SVG_NS,
    XLINK_NS,
    DocumentNotFound,
    InspectionError,
    is_element,
    local_name,
)
from inkscape_mcp.registry import DocEntry, Registry, get_registry
from inkscape_mcp.workspace.xml_safety import UnsafeXMLError, parse_svg_file

_INKSCAPE_LABEL = f"{{{INKSCAPE_NS}}}label"
_XLINK_HREF = f"{{{XLINK_NS}}}href"

#: Attributes that may carry a `#id` / `url(#id)` reference to another element (rename/dup remap).
_REF_ATTRS = (
    "fill",
    "stroke",
    "mask",
    "clip-path",
    "filter",
    "style",
    "marker-start",
    "marker-mid",
    "marker-end",
)

# --- Value validation patterns ----------------------------------------------

#: Hex colour: #rgb / #rgba / #rrggbb / #rrggbbaa.
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
#: rgb()/rgba()/hsl()/hsla() with an injection-safe inner charset (digits, %, ., ,, /, and only
#: horizontal whitespace — no newline/vertical whitespace, which could split a style declaration).
_FUNC_COLOR_RE = re.compile(r"^(?:rgb|rgba|hsl|hsla)\([0-9.,%/ \t]+\)$")
#: A bare colour keyword / named colour: letters only (can never inject CSS punctuation).
_NAME_COLOR_RE = re.compile(r"^[A-Za-z]{1,32}$")
#: A font-family value: letters/digits/space and a few safe separators; no CSS punctuation.
_FONT_FAMILY_RE = re.compile(r"^[A-Za-z0-9 ,._'\"-]{1,128}$")
#: A length: number + optional unit (px, pt, pc, mm, cm, in, em, ex, rem, %).
_LENGTH_RE = re.compile(r"^[0-9]*\.?[0-9]+(?:px|pt|pc|mm|cm|in|em|ex|rem|%)?$")
#: Allowed font-weight keywords (numeric 100-900 handled separately).
_FONT_WEIGHTS = frozenset(
    {
        "normal",
        "bold",
        "bolder",
        "lighter",
        "100",
        "200",
        "300",
        "400",
        "500",
        "600",
        "700",
        "800",
        "900",
    }
)
#: Inline-style declaration splitter (`prop:value;prop:value`).
_DECL_RE = re.compile(r"\s*([^:;]+?)\s*:\s*([^;]+?)\s*(?:;|$)")
#: A syntactically valid, injection-safe SVG id. XML Name-ish: starts with a letter or underscore,
#: then letters / digits / ``_ . : -``. Used to validate any client-supplied id (a `new_id`, a
#: creation `object_id`, a `<use>` href target) before it becomes an id and/or a remap target
#: written document-wide. Shared by the text/object and creation engines.
SAFE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")


class EditError(Exception):
    """An edit input was invalid or could not be applied.

    Carries a stable, host-path-free public message; the tool layer maps it to `ToolError`.
    """


class TargetNotFound(Exception):
    """A targeted object id does not exist in the document.

    Public message is stable and carries no host path.
    """


# --- Working-copy load / write ----------------------------------------------


def load_working_tree(
    doc_id: str, registry: Registry | None = None
) -> tuple[DocEntry, etree._ElementTree]:
    """Resolve `doc_id` and parse its WORKING COPY through the safe parser.

    Returns `(entry, tree)`. Raises `DocumentNotFound` for an unknown id and `InspectionError`
    if the working copy cannot be parsed safely (both carry stable, host-path-free messages).
    """
    reg = registry if registry is not None else get_registry()
    try:
        entry = reg.get(doc_id)
    except KeyError:
        raise DocumentNotFound("document id not found") from None
    try:
        tree = parse_svg_file(Path(entry.working_path))
    except UnsafeXMLError as exc:
        raise InspectionError("document could not be parsed safely") from exc
    return entry, tree


def write_working_tree(entry: DocEntry, tree: etree._ElementTree) -> None:
    """Serialize `tree` back over the WORKING COPY only (never the original source file)."""
    data = etree.tostring(tree, xml_declaration=True, encoding="UTF-8")
    # Temp file in the same dir + os.replace: a failed write never leaves a truncated working copy.
    target = Path(entry.working_path)
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(tmp, flags, 0o600), "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# --- Element helpers --------------------------------------------------------


def find_by_id(root: etree._Element, object_id: str) -> etree._Element | None:
    """Return the first element whose `id` equals `object_id`, or None."""
    for elem in root.iter():
        if is_element(elem) and elem.get("id") == object_id:
            return elem
    return None


def require_target(root: etree._Element, object_id: str) -> etree._Element:
    """Resolve a single object id or raise `TargetNotFound`."""
    elem = find_by_id(root, object_id)
    if elem is None:
        raise TargetNotFound("object id not found in document")
    return elem


def require_targets(root: etree._Element, object_ids: list[str]) -> list[etree._Element]:
    """Resolve every object id (preserving order) or raise `TargetNotFound` for the first miss."""
    if not object_ids:
        raise EditError("no target object ids supplied")
    return [require_target(root, oid) for oid in object_ids]


def all_ids(root: etree._Element) -> set[str]:
    """Every `id` value currently defined in the document."""
    ids: set[str] = set()
    for elem in root.iter():
        if is_element(elem):
            oid = elem.get("id")
            if oid:
                ids.add(oid)
    return ids


# --- Inline-style helpers ---------------------------------------------------


def parse_style(elem: etree._Element) -> dict[str, str]:
    """Parse the element's inline `style="…"` block into an ordered prop->value dict."""
    decls: dict[str, str] = {}
    style = elem.get("style")
    if not style:
        return decls
    for match in _DECL_RE.finditer(style):
        prop = match.group(1).strip().lower()
        value = match.group(2).strip()
        if prop:
            decls[prop] = value
    return decls


def _serialize_style(decls: dict[str, str]) -> str:
    return ";".join(f"{k}:{v}" for k, v in decls.items())


def set_style_property(
    elem: etree._Element,
    prop: str,
    value: str,
    *,
    key: Callable[[str], str] | None = None,
) -> bool:
    """Set CSS `prop` to `value` on the element's inline style, returning True if it changed.

    The value is written into the `style` attribute (which overrides a presentation attribute);
    any same-named presentation attribute is dropped so the two cannot diverge. `prop` and
    `value` are assumed already validated by the caller.

    No-op hygiene: if the element's EFFECTIVE current value of `prop` — the inline-style
    declaration if present, else the same-named presentation attribute — already equals `value`,
    NOTHING is written and `False` is returned. Equality is compared through `key` when given
    (e.g. :func:`color_key`, so `fill="#3366CC"` matches a requested `#3366cc`). Without this
    guard, re-applying the value an element already carries as a PRESENTATION ATTRIBUTE would
    migrate it into an inline `style` block — a byte change with no visual effect that the
    pipeline's serialization diff would miscount as a real change (the `set_fill`-to-same-colour
    false positive).
    """
    decls = parse_style(elem)
    current = decls[prop] if prop in decls else elem.get(prop)
    norm = key if key is not None else (lambda v: v)
    if current is not None and norm(current) == norm(value):
        return False
    decls[prop] = value
    elem.set("style", _serialize_style(decls))
    if prop in elem.attrib:
        del elem.attrib[prop]
    return True


# --- Colour validation / matching -------------------------------------------


def normalize_color(raw: str) -> str:
    """Validate and canonicalize a colour input, or raise `EditError`.

    Accepts hex (`#rgb`/`#rgba`/`#rrggbb`/`#rrggbbaa`), `rgb()/rgba()/hsl()/hsla()` with an
    injection-safe inner charset, and bare keywords/named colours (letters only, e.g. `none`,
    `red`, `currentColor`). Anything else — notably a value containing `;`, `{`, `}`, or other
    CSS punctuation — is rejected so it can never inject extra declarations into a `style` block.
    Hex is lowercased; named keywords are lowercased.
    """
    value = raw.strip()
    if not value:
        raise EditError("colour value is empty")
    if _HEX_RE.match(value):
        return value.lower()
    if _FUNC_COLOR_RE.match(value):
        return re.sub(r"\s+", "", value).lower()
    if _NAME_COLOR_RE.match(value):
        return value.lower()
    raise EditError(f"invalid colour value: {value!r}")


#: A paint-server reference: `url(#id)` (same-document only) with an optional fallback `<color>`.
#: The id must match the safe SVG-id charset (same as `SAFE_ID_RE`); an external/`javascript:` url
#: or any extra CSS punctuation never matches, so a paint value can neither inject markup nor fetch
#: a remote resource (sec.12).
_URL_PAINT_RE = re.compile(r"^url\(\s*#([A-Za-z_][A-Za-z0-9_.:-]*)\s*\)(?:\s+(\S.*))?$")


def normalize_paint(raw: str) -> str:
    """Validate a paint value (a colour OR a `url(#id)` reference), or raise `EditError`.

    Accepts everything :func:`normalize_color` accepts, PLUS a same-document `url(#id)` reference to
    a gradient / pattern in `<defs>` (e.g. an id returned by `add_linear_gradient`), optionally
    followed by a fallback colour (`url(#id) <color>`). The referenced id must match the safe
    SVG-id charset and the fallback (when present) is validated through :func:`normalize_color`; an
    external url, a `javascript:` value, or any extra CSS punctuation is rejected so a paint value
    can never inject extra declarations or fetch a remote resource (sec.12). The id is preserved
    verbatim (ids are case-sensitive); a colour is canonicalized as by `normalize_color`.
    """
    value = raw.strip()
    match = _URL_PAINT_RE.match(value)
    if match is None:
        return normalize_color(value)
    ref_id, fallback = match.group(1), match.group(2)
    if fallback is None:
        return f"url(#{ref_id})"
    return f"url(#{ref_id}) {normalize_color(fallback)}"


def is_url_paint(value: str) -> bool:
    """True if `value` is a `url(#id)` paint reference (vs a plain colour)."""
    return value.startswith("url(")


def color_key(raw: str) -> str:
    """A comparison key for a colour token (lowercased, 3/4-digit hex expanded to 6/8).

    Used to match a document colour against a requested `from` colour regardless of casing or
    hex shorthand. Non-colour tokens are returned lowercased as-is.
    """
    value = raw.strip().lower()
    if value.startswith("#"):
        hexpart = value[1:]
        if re.fullmatch(r"[0-9a-f]{3,4}", hexpart):
            hexpart = "".join(ch * 2 for ch in hexpart)
        return f"#{hexpart}"
    if re.match(r"^(?:rgb|rgba|hsl|hsla)\(", value):
        return re.sub(r"\s+", "", value)
    return value


def normalize_font_family(raw: str) -> str:
    """Validate a font-family value (injection-safe charset) or raise `EditError`."""
    value = raw.strip()
    if not _FONT_FAMILY_RE.match(value):
        raise EditError(f"invalid font-family value: {value!r}")
    return value


def normalize_length(raw: str) -> str:
    """Validate a CSS length (number + optional unit) or raise `EditError`."""
    value = raw.strip()
    if not _LENGTH_RE.match(value):
        raise EditError(f"invalid length value: {value!r}")
    return value


def normalize_font_weight(raw: str) -> str:
    """Validate a font-weight keyword / numeric value or raise `EditError`."""
    value = raw.strip().lower()
    if value not in _FONT_WEIGHTS:
        raise EditError(f"invalid font-weight value: {raw!r}")
    return value


# --- Numeric / transform helpers --------------------------------------------


def fmt_num(n: float) -> str:
    """Format a finite number for an attribute value (trailing zeros trimmed). Raises on NaN/inf."""
    if not math.isfinite(n):
        raise EditError("number must be finite")
    s = f"{n:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def require_finite(name: str, value: float) -> None:
    """Reject a non-finite numeric input with a stable, host-path-free :class:`EditError`."""
    if not math.isfinite(value):
        raise EditError(f"{name} must be finite")


def prepend_transform(elem: etree._Element, transform: str) -> None:
    """Prepend a transform function to the element's `transform` list (applied in parent space)."""
    existing = elem.get("transform")
    elem.set("transform", f"{transform} {existing}".strip() if existing else transform)


_NUMBER_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _attr_num(raw: str | None) -> float:
    """An attribute as a float (``None``/blank is 0.0); raises ``ValueError`` on garbage."""
    text = (raw or "").strip()
    return float(text) if text else 0.0


def _point_pairs(raw: str) -> list[tuple[float, float]]:
    nums = [float(n) for n in _NUMBER_RE.findall(raw)]
    return list(zip(nums[0::2], nums[1::2], strict=False))


#: Path data longer than this is not measured (bounds memory on untrusted documents).
_MAX_PATH_CHARS = 1 << 20

#: Numbers each path command consumes per repetition.
_PATH_ARITY = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}
_PATH_TOKEN_RE = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|" + _NUMBER_RE.pattern)


def path_points(d: str) -> list[tuple[float, float]]:
    """End and control points of a path ``d`` in absolute coordinates (all commands, rel/abs).

    The hull of these points contains the curve, so their box is a safe, slightly loose bbox.
    An arc adds its end point and the box of a radius-sized ellipse around its chord midpoint.
    Raises ``ValueError`` on malformed data or data over ``_MAX_PATH_CHARS``.
    """
    if len(d) > _MAX_PATH_CHARS:
        raise ValueError("path data too large to measure")
    tokens = _PATH_TOKEN_RE.findall(d)
    points: list[tuple[float, float]] = []
    x = y = start_x = start_y = 0.0
    i, cmd = 0, ""
    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
            if cmd in "Zz":
                x, y = start_x, start_y
                continue
        if not cmd or cmd in "Zz":
            raise ValueError("path data without a command")
        upper = cmd.upper()
        n = _PATH_ARITY[upper]
        args = [float(t) for t in tokens[i : i + n]]
        if len(args) != n:
            raise ValueError("truncated path data")
        i += n
        rel = cmd.islower()
        if upper == "H":
            x = args[0] + (x if rel else 0)
        elif upper == "V":
            y = args[0] + (y if rel else 0)
        elif upper == "A":
            rx, ry = abs(args[0]), abs(args[1])
            ex, ey = args[5] + (x if rel else 0), args[6] + (y if rel else 0)
            mx, my = (x + ex) / 2, (y + ey) / 2
            points += [(mx - rx, my - ry), (mx + rx, my + ry)]
            x, y = ex, ey
        else:
            pairs = list(zip(args[0::2], args[1::2], strict=True))
            pts = [(px + (x if rel else 0), py + (y if rel else 0)) for px, py in pairs]
            points += pts[:-1]
            x, y = pts[-1]
            if upper == "M":
                start_x, start_y = x, y
                cmd = "l" if rel else "L"  # further pairs after M are implicit line-tos
        points.append((x, y))
    return points


def content_bbox(elem: etree._Element) -> tuple[float, float, float, float] | None:
    """Best-effort ``(x, y, w, h)`` of a subtree in its OWN coordinate space (no engine).

    Unions the boxes readable straight from attributes: ``rect``/``image``/``use``, ``circle`` /
    ``ellipse``, ``line``, ``polygon`` / ``polyline`` points, a ``path`` (end + control points,
    see :func:`path_points`), and a ``text`` anchor
    ``x``/``y``. Descendant transforms are NOT applied (direct DOM, ADR-005), so this is a
    heuristic for pivots and scale-to-fit, not exact geometry. ``None`` when nothing is measurable.
    """
    xs: list[float] = []
    ys: list[float] = []

    def add(points: list[tuple[float, float]]) -> None:
        for x, y in points:
            if math.isfinite(x) and math.isfinite(y):
                xs.append(x)
                ys.append(y)

    for node in elem.iter():
        if not isinstance(node.tag, str):
            continue
        local = etree.QName(node.tag).localname
        try:
            if local in ("rect", "image", "use"):
                x, y = _attr_num(node.get("x")), _attr_num(node.get("y"))
                w, h = _attr_num(node.get("width")), _attr_num(node.get("height"))
                if w >= 0 and h >= 0:
                    add([(x, y), (x + w, y + h)])
            elif local in ("circle", "ellipse"):
                cx, cy = _attr_num(node.get("cx")), _attr_num(node.get("cy"))
                r = _attr_num(node.get("r"))
                rx = _attr_num(node.get("rx")) if local == "ellipse" else r
                ry = _attr_num(node.get("ry")) if local == "ellipse" else r
                add([(cx - rx, cy - ry), (cx + rx, cy + ry)])
            elif local == "line":
                add([(_attr_num(node.get(f"x{i}")), _attr_num(node.get(f"y{i}"))) for i in (1, 2)])
            elif local in ("polygon", "polyline"):
                add(_point_pairs(node.get("points") or ""))
            elif local == "path":
                add(path_points(node.get("d") or ""))
            elif local == "text" and node.get("x") is not None and node.get("y") is not None:
                add([(_attr_num(node.get("x")), _attr_num(node.get("y")))])
        except ValueError:
            continue

    if not xs:
        return None
    x0, y0 = min(xs), min(ys)
    w, h = max(xs) - x0, max(ys) - y0
    return (x0, y0, w, h) if math.isfinite(w) and math.isfinite(h) else None


# --- Reference remapping (rename / duplicate) -------------------------------


def _rewrite_ref_value(value: str, mapping: dict[str, str]) -> str:
    """Rewrite `#id` and `url(#id)` occurrences in an attribute/style value via `mapping`."""

    def repl_url(match: re.Match[str]) -> str:
        frag = match.group(1)
        return f"url(#{mapping.get(frag, frag)})"

    new = re.sub(r"url\(\s*#([^)\s]+)\s*\)", repl_url, value)
    if value.startswith("#"):
        frag = value[1:]
        if frag in mapping:
            new = f"#{mapping[frag]}"
    return new


def rewrite_references(root: etree._Element, mapping: dict[str, str]) -> None:
    """Rewrite all in-document references (`#id`, `url(#id)`, `href="#id"`) per `mapping`."""
    if not mapping:
        return
    for elem in root.iter():
        if not is_element(elem):
            continue
        for attr in (_XLINK_HREF, "href"):
            href = elem.get(attr)
            if href and href.startswith("#") and href[1:] in mapping:
                elem.set(attr, f"#{mapping[href[1:]]}")
        for attr in _REF_ATTRS:
            val = elem.get(attr)
            if val and ("url(" in val or val.startswith("#")):
                elem.set(attr, _rewrite_ref_value(val, mapping))


def set_label(elem: etree._Element, label: str) -> None:
    """Set the element's `inkscape:label`."""
    elem.set(_INKSCAPE_LABEL, label)


def _short_token() -> str:
    return secrets.token_hex(3)


def deep_copy_with_new_ids(
    elem: etree._Element, root: etree._Element, new_id: str | None
) -> tuple[etree._Element, str]:
    """Deep-copy `elem`, re-id every id in the clone uniquely, and rewrite intra-clone refs.

    The clone's top element id becomes `new_id` (validated unique by the caller) or a freshly
    suffixed id. Every descendant id is suffixed too, and `#id` / `url(#id)` references INSIDE
    the clone are rewritten so the copy is self-consistent. References from the original (and the
    rest of the document) are untouched. Returns `(clone, top_id)`.
    """
    clone = copy.deepcopy(elem)
    existing = all_ids(root)
    suffix = _short_token()

    mapping: dict[str, str] = {}
    top_old = clone.get("id")
    top_new = (
        new_id if new_id is not None else (f"{top_old}-{suffix}" if top_old else f"copy-{suffix}")
    )
    if top_new in existing:
        raise EditError(f"id already in use: {top_new!r}")
    if top_old:
        mapping[top_old] = top_new
    clone.set("id", top_new)
    existing.add(top_new)

    for node in clone.iter():
        if node is clone or not is_element(node):
            continue
        nid = node.get("id")
        if not nid:
            continue
        candidate = f"{nid}-{suffix}"
        while candidate in existing:
            candidate = f"{nid}-{_short_token()}"
        mapping[nid] = candidate
        node.set("id", candidate)
        existing.add(candidate)

    rewrite_references(clone, mapping)
    _scope_clone_css(clone, mapping, suffix)
    return clone, top_new


#: A CSS class / id token in a selector (a leading digit is a number like `.5em`, not a name).
_CSS_CLASS_RE = re.compile(r"\.(-?[_a-zA-Z][\w-]*)")
_CSS_ID_RE = re.compile(r"#(-?[_a-zA-Z][\w-]*)")
#: A selector prelude followed by its innermost `{ ... }` declaration block.
_CSS_RULE_RE = re.compile(r"([^{}]*)(\{[^{}]*\})")


def _scope_clone_css(clone: etree._Element, mapping: dict[str, str], suffix: str) -> None:
    """Keep a clone's OWN `<style>` rules bound to the clone, not to the whole document.

    CSS is document-global: two copies of a doc with `.accent { … }` would both obey whichever
    rule comes last. Classes the clone's own stylesheet defines get the clone's id suffix (in the
    selectors and in every `class` attribute of the clone), and `#id` selectors follow the id
    remap. Classes defined outside the clone are left alone, so a same-document copy keeps its
    styling.
    """
    styles = [n for n in clone.iter(f"{{{SVG_NS}}}style") if n.text]
    if not styles:
        return
    preludes = [m[1] for st in styles for m in _CSS_RULE_RE.finditer(st.text or "")]
    rename = {c: f"{c}-{suffix}" for p in preludes for c in _CSS_CLASS_RE.findall(p)}

    def scope(prelude: str) -> str:
        prelude = _CSS_CLASS_RE.sub(lambda m: "." + rename.get(m[1], m[1]), prelude)
        return _CSS_ID_RE.sub(lambda m: "#" + mapping.get(m[1], m[1]), prelude)

    for st in styles:
        st.text = _CSS_RULE_RE.sub(lambda m: scope(m[1]) + m[2], st.text or "")
    for node in clone.iter():
        if is_element(node) and node.get("class"):
            node.set("class", " ".join(rename.get(c, c) for c in node.get("class", "").split()))


def parse_viewbox(raw: str | None) -> list[float] | None:
    """Parse a `viewBox` string into four floats, or None if absent/malformed."""
    if not raw:
        return None
    parts = [p for p in re.split(r"[,\s]+", raw.strip()) if p != ""]
    if len(parts) != 4:
        return None
    nums: list[float] = []
    for part in parts:
        try:
            nums.append(float(part))
        except ValueError:
            return None
    return nums


__all__ = [
    "SAFE_ID_RE",
    "SVG_NS",
    "DocumentNotFound",
    "EditError",
    "InspectionError",
    "TargetNotFound",
    "all_ids",
    "color_key",
    "content_bbox",
    "deep_copy_with_new_ids",
    "find_by_id",
    "fmt_num",
    "is_element",
    "is_url_paint",
    "load_working_tree",
    "local_name",
    "normalize_color",
    "normalize_font_family",
    "normalize_font_weight",
    "normalize_length",
    "normalize_paint",
    "parse_style",
    "parse_viewbox",
    "path_points",
    "prepend_transform",
    "require_finite",
    "require_target",
    "require_targets",
    "rewrite_references",
    "set_label",
    "set_style_property",
    "write_working_tree",
]
