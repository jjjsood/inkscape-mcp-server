"""Direct-DOM element-creation / defs / grouping engines (, ADR-005).

Pure ``mutate(tree) -> str`` builders for the authoring surface: one engine per shape primitive
(``create_rect`` / ``create_circle`` / ``create_ellipse`` / ``create_line`` / ``create_polygon`` /
``create_polyline`` / ``create_path`` / ``create_text``), the gradient/defs builders
(``add_linear_gradient`` / ``add_radial_gradient``), and the structural builders (``create_group`` /
``group_objects`` / ``reparent_object`` / ``create_use``). Each function validates its inputs up
front (raising :class:`EditError` / :class:`TargetNotFound`), then returns a closure that edits the
parsed working tree IN MEMORY and returns a short human summary. None carries an ``@mcp.tool``
decorator; the thin tool layer wires them into the shared, reversible edit pipeline (`apply_edit`)
so every change gets a snapshot + Operation Record + before/after preview (ADR-004).

NO catch-all ``add_element(tag, attrs)`` — every tool is small and typed (ADR-002 / ADR-003). All
creation is direct lxml on the DOM (no Inkscape engine) per ADR-005, reusing the validated
primitives in :mod:`inkscape_mcp.edit.dom`.

SAFETY (sec.12): every client value that lands in an attribute is validated against a STRICT
pattern (numbers via :func:`dom.fmt_num`, colours via :func:`dom.normalize_color`, lengths via
:func:`dom.normalize_length`, ids via :data:`dom.SAFE_ID_RE`, path ``d`` via a command/charset
allowlist) so a caller can never inject extra markup, CSS declarations, control characters, or an
external / ``javascript:`` reference. ``create_use`` accepts ONLY a same-document ``#id`` reference;
text content is control-char-scrubbed; every numeric is finite-checked. The only client inputs that
reach this module are a ``doc_id``, object ids (used solely for in-tree lookups / ``#id`` refs,
never argv), and the typed creation parameters validated below.
"""

from __future__ import annotations

import re
import secrets

from lxml import etree

from inkscape_mcp.document.inspect import SVG_NS, XLINK_NS
from inkscape_mcp.edit.dom import (
    SAFE_ID_RE,
    EditError,
    all_ids,
    color_key,
    find_by_id,
    fmt_num,
    is_url_paint,
    normalize_color,
    normalize_length,
    normalize_paint,
    require_target,
    set_style_property,
)
from inkscape_mcp.edit.pipeline import MutateFn

_SVG = f"{{{SVG_NS}}}"
_XLINK_HREF = f"{{{XLINK_NS}}}href"

#: Maximum length of a path ``d`` string accepted by :func:`make_create_path` (characters). Bounds
#: an otherwise unbounded attribute write driven by client input; well above any realistic path.
_MAX_PATH_LEN = 200_000

#: Maximum text-content length accepted by :func:`make_create_text` (characters).
_MAX_TEXT_LEN = 100_000

#: Maximum number of points accepted by :func:`make_create_polygon` / :func:`make_create_polyline`.
_MAX_POINTS = 100_000

#: Maximum number of gradient stops accepted by the gradient builders.
_MAX_STOPS = 1_000

#: Control characters that must never enter text content (C0/C1 minus tab/newline/carriage return).
_FORBIDDEN_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

#: Allowed character set for a path ``d``: digits, whitespace, ``,``, ``.``, sign, exponent, and the
#: SVG path command letters. Anything else (`<`, `;`, `(`, letters outside the command set, ...) is
#: rejected so a ``d`` value can never carry markup or a function call. Geometry is NOT parsed.
_PATH_D_RE = re.compile(r"^[\d\s,.+\-eEMmLlHhVvCcSsQqTtAaZz]+$")

#: Maximum length of a ``transform`` value accepted by :func:`make_create_use` (characters).
_MAX_TRANSFORM_LEN = 2_000

#: A ``transform`` value: one or more allowed transform functions (numeric args only) separated by
#: whitespace / commas. Restricting the structure (not just a charset) means a caller cannot smuggle
#: arbitrary letters/markup into the attribute; geometry is NOT evaluated.
_TRANSFORM_RE = re.compile(
    r"^\s*(?:(?:matrix|translate|scale|rotate|skewX|skewY)\s*\([\d\s,.+\-eE]*\)\s*)+$"
)

#: An inkscape:groupmode="layer" group is the conventional default insert parent when present.
_INKSCAPE_GROUPMODE = "{http://www.inkscape.org/namespaces/inkscape}groupmode"


def _is_element(elem: etree._Element) -> bool:
    return isinstance(elem.tag, str)


def _local_name(elem: etree._Element) -> str:
    tag = elem.tag
    if not isinstance(tag, str):
        return ""
    return etree.QName(tag).localname


def _gen_id(prefix: str, existing: set[str]) -> str:
    """Mint a fresh, unused, charset-safe id with the given prefix."""
    while True:
        candidate = f"{prefix}-{secrets.token_hex(3)}"
        if candidate not in existing:
            return candidate


def _resolve_id(supplied: str | None, prefix: str, existing: set[str]) -> str:
    """Validate a client-supplied id (safe charset + unused) or mint a fresh one."""
    if supplied is None:
        return _gen_id(prefix, existing)
    if not SAFE_ID_RE.match(supplied):
        raise EditError(f"invalid object id: {supplied!r}")
    if supplied in existing:
        raise EditError(f"id already in use: {supplied!r}")
    return supplied


def _default_parent(root: etree._Element) -> etree._Element:
    """The default insert parent: the first ``inkscape:label`` layer if any, else the document root.

    Inserting into the first layer matches how a user-authored document is structured (content lives
    in a layer, not bare under ``<svg>``); a document with no layer falls back to the root so a
    plain SVG still works.
    """
    for child in root:
        if (
            _is_element(child)
            and _local_name(child) == "g"
            and child.get(_INKSCAPE_GROUPMODE) == "layer"
        ):
            return child
    return root


def _resolve_parent(root: etree._Element, parent_id: str | None) -> etree._Element:
    """Resolve the insert parent: an explicit ``parent_id`` (must exist) or the document default."""
    if parent_id is None:
        return _default_parent(root)
    return require_target(root, parent_id)


def _num(value: float, field: str) -> str:
    """Format a finite numeric attribute value, attributing a bad value to ``field``."""
    try:
        return fmt_num(float(value))
    except (TypeError, ValueError, EditError) as exc:
        raise EditError(f"invalid {field} value: {value!r}") from exc


def _positive(value: float, field: str) -> str:
    """Format a finite, strictly-positive numeric (radius / width / height)."""
    num = float(value)
    if num <= 0:
        raise EditError(f"{field} must be greater than 0")
    return _num(num, field)


def _validate_transform(raw: str) -> str:
    """Validate a ``transform`` value (allowed transform functions, numeric args) or raise.

    Length-bounded to ``_MAX_TRANSFORM_LEN`` and matched against ``_TRANSFORM_RE`` so it can carry
    only ``matrix/translate/scale/rotate/skewX/skewY`` calls with numeric arguments — never markup,
    arbitrary letters, or a function outside the SVG transform set (sec.12).
    """
    if len(raw) > _MAX_TRANSFORM_LEN:
        raise EditError(f"transform too long: {len(raw)} > {_MAX_TRANSFORM_LEN} characters")
    if not _TRANSFORM_RE.match(raw):
        raise EditError(f"invalid transform value: {raw!r}")
    return raw


def _non_negative(value: float, field: str) -> str:
    """Format a finite, non-negative numeric (size that may legitimately be 0)."""
    num = float(value)
    if num < 0:
        raise EditError(f"{field} must be 0 or greater")
    return _num(num, field)


def _append(parent: etree._Element, local: str, attrs: dict[str, str]) -> etree._Element:
    """Create an SVG element with the given (already-validated) attributes; append it to parent."""
    elem = etree.SubElement(parent, f"{_SVG}{local}")
    for key, val in attrs.items():
        elem.set(key, val)
    return elem


def _canon_inline_style(
    fill: str | None, stroke: str | None, stroke_width: str | None
) -> tuple[str | None, str | None, str | None]:
    """Validate optional inline paint EAGERLY, returning canonical ``(fill, stroke, sw)``.

    Mirrors how ``set_fill`` / ``set_stroke`` validate paint: ``fill`` / ``stroke`` go through
    :func:`dom.normalize_paint` (a colour OR a ``url(#id)`` paint-server reference; CSS-injection
    punctuation rejected), ``stroke_width`` through :func:`dom.normalize_length`. Each unset value
    stays ``None`` (current behaviour). Raising HERE (before the closure runs) means an invalid
    colour fails the call before any pipeline side effect, exactly like a bad geometry value.
    """
    safe_fill = None if fill is None else normalize_paint(fill)
    safe_stroke = None if stroke is None else normalize_paint(stroke)
    safe_sw = None if stroke_width is None else normalize_length(stroke_width)
    return safe_fill, safe_stroke, safe_sw


def _apply_inline_style(
    elem: etree._Element, fill: str | None, stroke: str | None, stroke_width: str | None
) -> None:
    """Apply the ALREADY-VALIDATED inline paint to ``elem`` via the SAME style engine path.

    Writes through :func:`dom.set_style_property` exactly as ``set_fill`` / ``set_stroke`` do (a
    ``url(#id)`` ref is compared verbatim, a colour via :func:`dom.color_key`), so created-shape
    styling and the standalone style tools share one paint code path — no duplicated paint logic.
    """
    if fill is not None:
        set_style_property(elem, "fill", fill, key=None if is_url_paint(fill) else color_key)
    if stroke is not None:
        set_style_property(elem, "stroke", stroke, key=None if is_url_paint(stroke) else color_key)
    if stroke_width is not None:
        set_style_property(elem, "stroke-width", stroke_width)


# --- Shape primitives ----------------------------------------------


def make_create_rect(
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    parent_id: str | None = None,
    object_id: str | None = None,
    rx: float | None = None,
    ry: float | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
) -> MutateFn:
    """Build a ``mutate`` closure that inserts a ``<rect>``.

    ``x`` / ``y`` are the top-left corner, ``width`` / ``height`` the size (both must be > 0); the
    optional ``rx`` / ``ry`` are corner radii (≥ 0). The rect is appended to ``parent_id`` (must
    exist) or the document default parent (first layer, else root). Every numeric is finite-checked
    and formatted via :func:`dom.fmt_num` so no garbage token can land in an attribute. Optional
    ``fill`` / ``stroke`` / ``stroke_width`` paint it in the same call, validated like
    ``set_fill`` / ``set_stroke``.
    """
    sx, sy = _num(x, "x"), _num(y, "y")
    sw, sh = _positive(width, "width"), _positive(height, "height")
    srx = None if rx is None else _non_negative(rx, "rx")
    sry = None if ry is None else _non_negative(ry, "ry")
    style = _canon_inline_style(fill, stroke, stroke_width)

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        parent = _resolve_parent(root, parent_id)
        new_id = _resolve_id(object_id, "rect", all_ids(root))
        attrs = {"id": new_id, "x": sx, "y": sy, "width": sw, "height": sh}
        if srx is not None:
            attrs["rx"] = srx
        if sry is not None:
            attrs["ry"] = sry
        elem = _append(parent, "rect", attrs)
        _apply_inline_style(elem, *style)
        return f"created <rect> {new_id!r}"

    return mutate


def make_create_circle(
    cx: float,
    cy: float,
    r: float,
    *,
    parent_id: str | None = None,
    object_id: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
) -> MutateFn:
    """Build a ``mutate`` closure that inserts a ``<circle>`` at ``(cx, cy)``, radius r (> 0)."""
    scx, scy = _num(cx, "cx"), _num(cy, "cy")
    sr = _positive(r, "r")
    style = _canon_inline_style(fill, stroke, stroke_width)

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        parent = _resolve_parent(root, parent_id)
        new_id = _resolve_id(object_id, "circle", all_ids(root))
        elem = _append(parent, "circle", {"id": new_id, "cx": scx, "cy": scy, "r": sr})
        _apply_inline_style(elem, *style)
        return f"created <circle> {new_id!r}"

    return mutate


def make_create_ellipse(
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    *,
    parent_id: str | None = None,
    object_id: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
) -> MutateFn:
    """Build a ``mutate`` closure that inserts an ``<ellipse>`` at ``(cx, cy)`` with radii rx/ry."""
    scx, scy = _num(cx, "cx"), _num(cy, "cy")
    srx, sry = _positive(rx, "rx"), _positive(ry, "ry")
    style = _canon_inline_style(fill, stroke, stroke_width)

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        parent = _resolve_parent(root, parent_id)
        new_id = _resolve_id(object_id, "ellipse", all_ids(root))
        attrs = {"id": new_id, "cx": scx, "cy": scy, "rx": srx, "ry": sry}
        elem = _append(parent, "ellipse", attrs)
        _apply_inline_style(elem, *style)
        return f"created <ellipse> {new_id!r}"

    return mutate


def make_create_line(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    parent_id: str | None = None,
    object_id: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
) -> MutateFn:
    """Build a ``mutate`` closure that inserts a ``<line>`` from ``(x1, y1)`` to ``(x2, y2)``."""
    sx1, sy1 = _num(x1, "x1"), _num(y1, "y1")
    sx2, sy2 = _num(x2, "x2"), _num(y2, "y2")
    style = _canon_inline_style(fill, stroke, stroke_width)

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        parent = _resolve_parent(root, parent_id)
        new_id = _resolve_id(object_id, "line", all_ids(root))
        elem = _append(parent, "line", {"id": new_id, "x1": sx1, "y1": sy1, "x2": sx2, "y2": sy2})
        _apply_inline_style(elem, *style)
        return f"created <line> {new_id!r}"

    return mutate


def _validate_points(points: list[tuple[float, float]], field: str) -> tuple[str, list[float]]:
    """Validate a point list and return ``(svg_points_attr, flat_coords)``.

    Each coordinate is finite-checked via :func:`dom.fmt_num`. Returns the canonical ``x,y x,y``
    attribute string plus the flat coordinate list (for analytic bbox). Raises on emptiness / a bad
    coordinate / an over-cap list.
    """
    if not points:
        raise EditError(f"{field} requires at least one point")
    if len(points) > _MAX_POINTS:
        raise EditError(f"too many points: {len(points)} > {_MAX_POINTS}")
    parts: list[str] = []
    flat: list[float] = []
    for px, py in points:
        sx, sy = _num(px, "point x"), _num(py, "point y")
        parts.append(f"{sx},{sy}")
        flat.extend((float(px), float(py)))
    return " ".join(parts), flat


def make_create_polygon(
    points: list[tuple[float, float]],
    *,
    parent_id: str | None = None,
    object_id: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
) -> MutateFn:
    """Build a ``mutate`` closure that inserts a closed ``<polygon>`` from ``points``."""
    points_attr, _ = _validate_points(points, "polygon")
    style = _canon_inline_style(fill, stroke, stroke_width)

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        parent = _resolve_parent(root, parent_id)
        new_id = _resolve_id(object_id, "polygon", all_ids(root))
        elem = _append(parent, "polygon", {"id": new_id, "points": points_attr})
        _apply_inline_style(elem, *style)
        return f"created <polygon> {new_id!r}"

    return mutate


def make_create_polyline(
    points: list[tuple[float, float]],
    *,
    parent_id: str | None = None,
    object_id: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
) -> MutateFn:
    """Build a ``mutate`` closure that inserts an open ``<polyline>`` from ``points``."""
    points_attr, _ = _validate_points(points, "polyline")
    style = _canon_inline_style(fill, stroke, stroke_width)

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        parent = _resolve_parent(root, parent_id)
        new_id = _resolve_id(object_id, "polyline", all_ids(root))
        elem = _append(parent, "polyline", {"id": new_id, "points": points_attr})
        _apply_inline_style(elem, *style)
        return f"created <polyline> {new_id!r}"

    return mutate


def make_create_path(
    d: str,
    *,
    parent_id: str | None = None,
    object_id: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
) -> MutateFn:
    """Build a ``mutate`` closure that inserts a ``<path>`` with the validated ``d``.

    ``d`` is validated against a STRICT charset — digits, whitespace, ``,``, ``.``, sign, exponent,
    and the SVG path command letters (``MmLlHhVvCcSsQqTtAaZz``) only — and length-bounded; geometry
    is NOT fully parsed. Anything else (markup, a function call, an unexpected letter) is rejected
    so the value can never inject content. The new path has no analytically-cheap bbox, so the tool
    layer reports ``bbox=None`` for paths.
    """
    raw = d.strip()
    if not raw:
        raise EditError("path d is empty")
    if len(raw) > _MAX_PATH_LEN:
        raise EditError(f"path d too long: {len(raw)} > {_MAX_PATH_LEN} characters")
    if not _PATH_D_RE.match(raw):
        raise EditError("path d contains invalid characters")
    style = _canon_inline_style(fill, stroke, stroke_width)

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        parent = _resolve_parent(root, parent_id)
        new_id = _resolve_id(object_id, "path", all_ids(root))
        elem = _append(parent, "path", {"id": new_id, "d": raw})
        _apply_inline_style(elem, *style)
        return f"created <path> {new_id!r}"

    return mutate


def make_create_text(
    x: float,
    y: float,
    text: str,
    *,
    parent_id: str | None = None,
    object_id: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
) -> MutateFn:
    """Build a ``mutate`` closure that inserts a ``<text>`` anchored at ``(x, y)``.

    ``text`` is length-bounded and may not contain control characters other than tab / newline /
    carriage return; it is assigned via lxml ``.text`` (stored as a text node) so no markup
    injection is possible. Text has no analytically-cheap bbox, so the tool layer reports
    ``bbox=None`` for text.
    """
    sx, sy = _num(x, "x"), _num(y, "y")
    if len(text) > _MAX_TEXT_LEN:
        raise EditError(f"text too long: {len(text)} > {_MAX_TEXT_LEN} characters")
    if _FORBIDDEN_CTRL_RE.search(text):
        raise EditError("text contains forbidden control characters")
    style = _canon_inline_style(fill, stroke, stroke_width)

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        parent = _resolve_parent(root, parent_id)
        new_id = _resolve_id(object_id, "text", all_ids(root))
        elem = _append(parent, "text", {"id": new_id, "x": sx, "y": sy})
        _apply_inline_style(elem, *style)
        elem.text = text
        return f"created <text> {new_id!r}"

    return mutate


# --- defs / gradients ----------------------------------------------


def _ensure_defs(root: etree._Element) -> etree._Element:
    """Return the document's ``<defs>``, creating one as the FIRST child of the root if absent."""
    for child in root:
        if _is_element(child) and _local_name(child) == "defs":
            return child
    defs = etree.Element(f"{_SVG}defs")
    root.insert(0, defs)
    return defs


def _validate_offset(raw: str) -> str:
    """Validate a gradient-stop offset: a number in ``[0, 1]`` or a percentage in ``[0%, 100%]``."""
    text = raw.strip()
    if text.endswith("%"):
        try:
            pct = float(text[:-1])
        except ValueError as exc:
            raise EditError(f"invalid stop offset: {raw!r}") from exc
        if not (0.0 <= pct <= 100.0):
            raise EditError("stop offset percentage must be between 0% and 100%")
        return f"{fmt_num(pct)}%"
    try:
        num = float(text)
    except ValueError as exc:
        raise EditError(f"invalid stop offset: {raw!r}") from exc
    if not (0.0 <= num <= 1.0):
        raise EditError("stop offset must be between 0 and 1")
    return fmt_num(num)


def _validate_stop_opacity(value: float) -> str:
    """Validate a stop opacity is a finite number in ``[0, 1]`` and format it."""
    num = float(value)
    if not (0.0 <= num <= 1.0):
        raise EditError("stop opacity must be between 0 and 1")
    return fmt_num(num)


def _coordinate(raw: str, field: str) -> str:
    """Validate a gradient coordinate: a plain number or a percentage. Injection-safe."""
    text = raw.strip()
    if text.endswith("%"):
        body = text[:-1]
        try:
            float(body)
        except ValueError as exc:
            raise EditError(f"invalid {field} value: {raw!r}") from exc
        return f"{fmt_num(float(body))}%"
    try:
        return fmt_num(float(text))
    except (ValueError, EditError) as exc:
        raise EditError(f"invalid {field} value: {raw!r}") from exc


def _build_stops(grad: etree._Element, stops: list[dict[str, object]]) -> None:
    """Append validated ``<stop>`` children to ``grad`` from a typed stop list.

    Each stop is ``{offset, color, opacity?}``. ``offset`` is a 0..1 / 0%..100% value, ``color`` is
    validated via :func:`dom.normalize_color` (CSS-injection punctuation rejected), and the optional
    ``opacity`` is a finite 0..1 value. Colour/opacity land in a ``style`` block written from
    validated tokens only.
    """
    if not stops:
        raise EditError("gradient requires at least one stop")
    if len(stops) > _MAX_STOPS:
        raise EditError(f"too many stops: {len(stops)} > {_MAX_STOPS}")
    for stop in stops:
        offset_raw = stop.get("offset")
        color_raw = stop.get("color")
        if offset_raw is None:
            raise EditError("gradient stop missing 'offset'")
        if color_raw is None:
            raise EditError("gradient stop missing 'color'")
        offset = _validate_offset(str(offset_raw))
        color = normalize_color(str(color_raw))
        decls = [f"stop-color:{color}"]
        opacity_raw = stop.get("opacity")
        if opacity_raw is not None:
            decls.append(f"stop-opacity:{_validate_stop_opacity(float(opacity_raw))}")  # type: ignore[arg-type]
        stop_el = etree.SubElement(grad, f"{_SVG}stop")
        stop_el.set("offset", offset)
        stop_el.set("style", ";".join(decls))


def make_add_linear_gradient(
    stops: list[dict[str, object]],
    *,
    x1: str = "0%",
    y1: str = "0%",
    x2: str = "100%",
    y2: str = "0%",
    object_id: str | None = None,
) -> MutateFn:
    """Build a ``mutate`` closure that adds a ``<linearGradient>`` to ``<defs>``.

    The vector is ``(x1, y1) -> (x2, y2)`` (each a number or percentage, default a left-to-right
    horizontal sweep). ``stops`` is a list of ``{offset, color, opacity?}`` (≥ 1). The gradient is
    appended to the document ``<defs>`` (created if absent); its id is returned and is usable as a
    ``url(#id)`` paint. A supplied ``object_id`` is validated (safe charset, unused) else minted.
    """
    sx1, sy1 = _coordinate(x1, "x1"), _coordinate(y1, "y1")
    sx2, sy2 = _coordinate(x2, "x2"), _coordinate(y2, "y2")
    # Validate stops eagerly so a bad input fails before any pipeline side effect.
    _build_stops(etree.Element(f"{_SVG}linearGradient"), stops)

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        new_id = _resolve_id(object_id, "lg", all_ids(root))
        defs = _ensure_defs(root)
        grad = etree.SubElement(defs, f"{_SVG}linearGradient")
        grad.set("id", new_id)
        grad.set("x1", sx1)
        grad.set("y1", sy1)
        grad.set("x2", sx2)
        grad.set("y2", sy2)
        _build_stops(grad, stops)
        return f"created <linearGradient> {new_id!r}"

    return mutate


def make_add_radial_gradient(
    stops: list[dict[str, object]],
    *,
    cx: str = "50%",
    cy: str = "50%",
    r: str = "50%",
    fx: str | None = None,
    fy: str | None = None,
    object_id: str | None = None,
) -> MutateFn:
    """Build a ``mutate`` closure that adds a ``<radialGradient>`` to ``<defs>``.

    The gradient is centred at ``(cx, cy)`` with radius ``r`` (each a number or percentage, default
    a centred 50%/50% circle); ``fx`` / ``fy`` optionally set the focal point. ``stops`` is a list
    of ``{offset, color, opacity?}`` (≥ 1). The gradient is appended to the document ``<defs>``
    (created if absent); its id is returned and is usable as a ``url(#id)`` paint.
    """
    scx, scy, sr = _coordinate(cx, "cx"), _coordinate(cy, "cy"), _coordinate(r, "r")
    sfx = None if fx is None else _coordinate(fx, "fx")
    sfy = None if fy is None else _coordinate(fy, "fy")
    _build_stops(etree.Element(f"{_SVG}radialGradient"), stops)

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        new_id = _resolve_id(object_id, "rg", all_ids(root))
        defs = _ensure_defs(root)
        grad = etree.SubElement(defs, f"{_SVG}radialGradient")
        grad.set("id", new_id)
        grad.set("cx", scx)
        grad.set("cy", scy)
        grad.set("r", sr)
        if sfx is not None:
            grad.set("fx", sfx)
        if sfy is not None:
            grad.set("fy", sfy)
        _build_stops(grad, stops)
        return f"created <radialGradient> {new_id!r}"

    return mutate


# --- grouping / symbols --------------------------------------------


def make_create_group(
    *,
    parent_id: str | None = None,
    object_id: str | None = None,
) -> MutateFn:
    """Build a ``mutate`` closure that inserts an empty ``<g>`` group.

    The group is appended to ``parent_id`` (must exist) or the document default parent. Use
    :func:`make_group_objects` to wrap EXISTING objects in a new group; this creates an empty one to
    populate later (e.g. by re-parenting objects into it).
    """

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        parent = _resolve_parent(root, parent_id)
        new_id = _resolve_id(object_id, "g", all_ids(root))
        _append(parent, "g", {"id": new_id})
        return f"created <g> {new_id!r}"

    return mutate


def make_group_objects(
    object_ids: list[str],
    *,
    object_id: str | None = None,
) -> MutateFn:
    """Build a ``mutate`` closure that wraps existing objects in a NEW ``<g>``.

    Every id in ``object_ids`` must exist (≥ 1) and they are moved (in document order) into a fresh
    group inserted at the position of the FIRST target in that target's parent — so the group takes
    the place of the objects it now contains. The new group's id is returned. The objects keep their
    own transforms / styles; only their parent changes.
    """
    if not object_ids:
        raise EditError("group_objects requires at least one object id")

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        targets = [require_target(root, oid) for oid in object_ids]
        first = targets[0]
        anchor_parent = first.getparent()
        if anchor_parent is None:
            raise EditError("cannot group the document root")
        new_id = _resolve_id(object_id, "g", all_ids(root))
        group = etree.Element(f"{_SVG}g")
        group.set("id", new_id)
        anchor_parent.insert(anchor_parent.index(first), group)
        for elem in targets:
            parent = elem.getparent()
            if parent is not None:
                parent.remove(elem)
            group.append(elem)
        return f"grouped {len(targets)} object(s) into {new_id!r}"

    return mutate


def make_reparent_object(
    object_id: str,
    new_parent_id: str,
) -> MutateFn:
    """Build a ``mutate`` closure that moves an object under a new parent.

    ``object_id`` is detached from its current parent and APPENDED to ``new_parent_id`` (both must
    exist). The move is rejected if the new parent is the object itself or one of its descendants
    (that would detach the subtree from the document). The object keeps its own attributes; only its
    parent changes. NOTE: re-parenting changes the inherited coordinate space — an object's visual
    position can shift if the old and new parents carry different transforms.
    """

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        elem = require_target(root, object_id)
        new_parent = require_target(root, new_parent_id)
        if new_parent is elem:
            raise EditError("cannot reparent an object under itself")
        for node in elem.iter():
            if node is new_parent:
                raise EditError("cannot reparent an object under its own descendant")
        old_parent = elem.getparent()
        if old_parent is None:
            raise EditError("cannot reparent the document root")
        old_parent.remove(elem)
        new_parent.append(elem)
        return f"reparented {object_id!r} under {new_parent_id!r}"

    return mutate


def make_create_use(
    href_id: str,
    *,
    parent_id: str | None = None,
    object_id: str | None = None,
    x: float | None = None,
    y: float | None = None,
    transform: str | None = None,
) -> MutateFn:
    """Build a ``mutate`` closure that inserts a ``<use href="#id">`` to an existing object.

    ``href_id`` is the id of the element to reference; it MUST be a same-document id (validated
    against the safe-id charset and required to exist). External / ``javascript:`` / ``url(...)``
    references are NOT accepted — only a same-document ``#id``. The reference is written as both
    ``xlink:href`` and ``href`` for broad compatibility.

    Placement semantics (the translate-scaling trap): ``<use>`` honours its own ``x`` / ``y`` as a
    translation in user space, applied BEFORE its ``transform``. Combining ``x`` / ``y`` with a
    ``transform="scale(...)"`` therefore SCALES that translation too (a ``scale(2)`` with ``x="10"``
    shifts by 20, not 10). To avoid the surprise, prefer EITHER ``x`` / ``y`` alone for a pure
    translation OR fold the translation into ``transform`` (e.g. ``translate(10,0) scale(2)``) — do
    not mix ``x`` / ``y`` with a scaling ``transform``. ``transform``, when given, is validated to
    be a sequence of allowed transform functions with numeric args only (length-bounded; no markup
    / arbitrary letters); ``x`` / ``y`` are finite-checked.
    """
    if not SAFE_ID_RE.match(href_id):
        raise EditError(f"invalid href id: {href_id!r}")
    sx = None if x is None else _num(x, "x")
    sy = None if y is None else _num(y, "y")
    st = None if transform is None else _validate_transform(transform)

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        if find_by_id(root, href_id) is None:
            raise EditError(f"href target {href_id!r} not found in document")
        parent = _resolve_parent(root, parent_id)
        new_id = _resolve_id(object_id, "use", all_ids(root))
        use = etree.SubElement(parent, f"{_SVG}use")
        use.set("id", new_id)
        use.set(_XLINK_HREF, f"#{href_id}")
        use.set("href", f"#{href_id}")
        if sx is not None:
            use.set("x", sx)
        if sy is not None:
            use.set("y", sy)
        if st is not None:
            use.set("transform", st)
        return f"created <use> {new_id!r} -> #{href_id}"

    return mutate


__all__ = [
    "make_add_linear_gradient",
    "make_add_radial_gradient",
    "make_create_circle",
    "make_create_ellipse",
    "make_create_group",
    "make_create_line",
    "make_create_path",
    "make_create_polygon",
    "make_create_polyline",
    "make_create_rect",
    "make_create_text",
    "make_create_use",
    "make_group_objects",
    "make_reparent_object",
]
