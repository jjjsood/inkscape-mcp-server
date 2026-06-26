"""Reusable SVG inspection engine (ADR-005 direct DOM).

Pure functions over the WORKING COPY (`DocEntry.working_path`), parsed once per call
through the normative safe parser (`parse_svg_file`). No MCP decorators here; the tool
layer (`inkscape_mcp.tools.document`) wraps these and maps errors to `ToolError`.

These models + function names are a CONTRACT consumed by (resources) and
(validate) — do not rename.

Namespaces handled: SVG (`http://www.w3.org/2000/svg`), Inkscape and sodipodi (layers,
labels), and xlink (`<image>` / `<use>` hrefs).

Definitions chosen here (documented for downstream reuse):

- **object** (`inspect_objects`): every element node in the SVG namespace that is *not*
  structural-only — i.e. excluding `svg`, `defs`, `metadata`, `style`, `title`, `desc`,
  and `sodipodi:namedview`. A `<g>` (group/layer) is kept because it is a selectable,
  transformable node. Each object carries its `id`, local `tag`, `inkscape:label`, and
  whether it has any styling (`style=` attribute or a `fill`/`stroke` presentation attr).
- **external asset** (`AssetInfo.external`): an `href` that is neither a `#fragment`
  (in-document reference) nor a `data:` URI — i.e. it points at a separate file or URL
  on disk/network. `<image>` and external `<use>` refs are the common cases.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from lxml import etree
from pydantic import BaseModel, Field

from inkscape_mcp.logging_setup import get_logger
from inkscape_mcp.registry import DocEntry, get_registry
from inkscape_mcp.workspace.subprocess_exec import (
    ProcessError,
    ProcessResult,
    run_inkscape,
    run_process,
)
from inkscape_mcp.workspace.xml_safety import UnsafeXMLError, parse_svg_file

_logger = get_logger("document.inspect")

# --- Namespaces -------------------------------------------------------------

SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
SODIPODI_NS = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.0.dtd"
XLINK_NS = "http://www.w3.org/1999/xlink"

_INKSCAPE_LABEL = f"{{{INKSCAPE_NS}}}label"
_INKSCAPE_GROUPMODE = f"{{{INKSCAPE_NS}}}groupmode"
_SODIPODI_INSENSITIVE = f"{{{SODIPODI_NS}}}insensitive"
_XLINK_HREF = f"{{{XLINK_NS}}}href"

# Structural-only SVG elements that are not "objects" (see module docstring).
_NON_OBJECT_TAGS = frozenset({"svg", "defs", "metadata", "style", "title", "desc", "namedview"})

# Presentation attributes that indicate inline styling on an element.
_STYLE_PRESENTATION_ATTRS = ("fill", "stroke")

# Generic CSS font keywords that always resolve — never reported as "missing". Shared with the
# validation engine (which imports this set) so font detection stays single-source.
GENERIC_FONT_KEYWORDS = frozenset(
    {
        "serif",
        "sans-serif",
        "monospace",
        "cursive",
        "fantasy",
        "system-ui",
        "ui-serif",
        "ui-sans-serif",
        "ui-monospace",
        "ui-rounded",
        "math",
        "emoji",
        "fangsong",
    }
)

# SVG element tags whose own attributes geometrically define an axis-aligned box without the render
# engine (direct DOM, ADR-005). Paths / text / groups / transformed elements report bbox=None — a
# true geometric bbox for those needs the Inkscape engine, out of scope for this read-only DOM
# surface. polygon / polyline boxes are derived analytically from their `points` list.
_BBOXABLE_TAGS = frozenset(
    {"rect", "image", "use", "circle", "ellipse", "line", "polygon", "polyline"}
)

# Matches `url(#id)` and `url(path)` references inside attribute/style values.
_URL_REF_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)")
# Splits a CSS-ish `prop:value;prop:value` inline style declaration block.
_DECL_RE = re.compile(r"\s*([^:;]+?)\s*:\s*([^;]+?)\s*(?:;|$)")
# Splits a <style> body into `selectors { declarations }` rule blocks (after comment stripping).
_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
# A single simple selector token we resolve: `tag`, `.class`, or `#id` (compound/descendant
# selectors are split on whitespace; the LAST simple token is matched against the element).
_CSS_SIMPLE_SELECTOR_RE = re.compile(r"^(?:([A-Za-z][\w-]*)|\.([\w-]+)|#([\w-]+)|\*)$")


class DocumentNotFound(Exception):
    """No registered document for the given id (library-level; tool layer maps to ToolError)."""


class InspectionError(Exception):
    """The working copy could not be parsed safely (malformed / unsafe XML).

    Public message is stable and carries no host path.
    """


# --- Models -----------------------------------------------------------------


class DocSummary(BaseModel):
    """Top-level document summary (viewBox / page / size / counts)."""

    doc_id: str
    width: str | None
    height: str | None
    units: str | None
    viewbox: list[float] | None
    page_count: int
    num_objects: int
    num_layers: int
    root_tag: str


class BBox(BaseModel):
    """An axis-aligned bounding box in user units (origin + extent), derived from an element's
    own geometry attributes via direct DOM. Element transforms and path geometry are NOT applied;
    elements whose box is not derivable from attributes report no bbox (the field is None)."""

    x: float
    y: float
    width: float
    height: float


class PaintInfo(BaseModel):
    """Resolved paint for one element: its effective fill, stroke, and stroke-width.

    Inline `style="…"` declarations win over presentation attributes (CSS specificity). Each value
    is the raw token as authored (e.g. `#ff0000`, `none`, `url(#grad)`), or None when unset on the
    element itself (no cascade/inheritance is resolved — this is a per-element view). `stroke_only`
    is True when the element paints a stroke but explicitly no fill (answers DoD check S13)."""

    fill: str | None = None
    stroke: str | None = None
    stroke_width: str | None = None
    stroke_only: bool = False


class TreeNode(BaseModel):
    """One node in the element tree.

    Carries the local `tag`, `id`, `inkscape:label`, child nodes, plus a per-element `paint`
    summary and structural flags (`is_layer`, `is_leaf`) so DoD checks "no stroke-only element"
    (S13) and "leaf objects only" (S12) are answerable from `inspect_document` without reading the
    SVG off disk. `is_leaf` is True when the node has no element children. `bbox` is the element's
    attribute-derived axis-aligned box (None when not derivable without the render engine — see
    `BBox`); it mirrors `ObjectInfo.bbox` exactly so an agent can answer "where is element X" from
    the tree alone, with no disk access."""

    tag: str
    id: str | None
    label: str | None
    paint: PaintInfo
    is_layer: bool
    is_leaf: bool
    bbox: BBox | None
    children: list[TreeNode]


class DocTree(BaseModel):
    """The full element tree rooted at the SVG root."""

    doc_id: str
    root: TreeNode


class LayerInfo(BaseModel):
    """An Inkscape layer: `<g inkscape:groupmode="layer">`."""

    id: str | None
    label: str | None
    visible: bool
    locked: bool
    num_children: int


class DocLayers(BaseModel):
    """All Inkscape layers in the document."""

    doc_id: str
    layers: list[LayerInfo]


class ObjectInfo(BaseModel):
    """One drawable/selectable object node (see module docstring for the definition).

    `paint`, `is_layer`, and `is_leaf` mirror the tree node so the same per-element answers (S12 /
    S13) are available from the flattened `objects` view. `bbox` is the element's attribute-derived
    axis-aligned box (None when not derivable without the render engine — see `BBox`)."""

    id: str | None
    tag: str
    label: str | None
    has_style: bool
    paint: PaintInfo = Field(default_factory=PaintInfo)
    is_layer: bool = False
    is_leaf: bool = True
    bbox: BBox | None = None


class DocObjects(BaseModel):
    """Flattened list of object nodes."""

    doc_id: str
    objects: list[ObjectInfo]


class ObjectRef(BaseModel):
    """A compact, addressable reference to one object — the shape `find_objects` returns and the
    shape `inspect_document.objects` exposes.

    `object_id` is the element's `id` (objects with no id are excluded — they cannot be addressed by
    the id-taking edit tools, which is the whole point of this surface). `tag` is the local element
    name. `bbox` is the element's attribute-derived axis-aligned box (None for path / text / group /
    transformed elements — see :class:`BBox`). `fill` / `stroke` are the EFFECTIVE per-element paint
    tokens as authored (inline `style` wins over the presentation attribute; None when unset on the
    element itself — no cascade). `text` is the element's concatenated text content (present only
    when non-empty)."""

    object_id: str
    tag: str
    bbox: BBox | None = None
    fill: str | None = None
    stroke: str | None = None
    text: str | None = None


class FindResult(BaseModel):
    """Result of `find_objects`: the matching object references plus the total count."""

    doc_id: str
    objects: list[ObjectRef]
    count: int


class DocStyles(BaseModel):
    """Distinct colors plus inline-style / CSS-rule counts."""

    doc_id: str
    colors: list[str]
    inline_style_count: int
    css_rule_count: int


class FontInfo(BaseModel):
    """A font-family value and how many times it was seen.

    `available` flags whether the family resolves against the installed font database (`fc-list`),
    using the SAME detection the V3 validation check uses; it is None when the database could not be
    queried (the check was skipped, not "missing"), and True for generic CSS keywords. `used_by` is
    the id of the first element that references this family (None for anonymous elements)."""

    family: str
    count: int
    available: bool | None
    used_by: str | None


class DocFonts(BaseModel):
    """All font-family values found across inline styles, `<style>`, and attrs."""

    doc_id: str
    fonts: list[FontInfo]


class AssetInfo(BaseModel):
    """A referenced asset: image / use / other, with its href and external flag.

    `used_by` is the id of the first element that references this asset (None when that element has
    no id), so an agent can trace an asset back to its referrer."""

    kind: str
    href: str
    external: bool
    used_by: str | None


class DocAssets(BaseModel):
    """All asset references (images, uses, external url() refs)."""

    doc_id: str
    assets: list[AssetInfo]


TreeNode.model_rebuild()


# --- Internal helpers -------------------------------------------------------


def _local_name(elem: etree._Element) -> str:
    """Local tag name of an element (namespace stripped). '' for comments/PIs."""
    tag = elem.tag
    if not isinstance(tag, str):  # comment / PI / entity
        return ""
    return etree.QName(tag).localname


def _is_element(elem: etree._Element) -> bool:
    """True for real element nodes (excludes comments / processing instructions)."""
    return isinstance(elem.tag, str)


def _label_of(elem: etree._Element) -> str | None:
    return elem.get(_INKSCAPE_LABEL)


def _id_of(elem: etree._Element) -> str | None:
    return elem.get("id")


def _is_layer(elem: etree._Element) -> bool:
    return _local_name(elem) == "g" and elem.get(_INKSCAPE_GROUPMODE) == "layer"


def _has_style(elem: etree._Element) -> bool:
    if elem.get("style"):
        return True
    return any(elem.get(attr) for attr in _STYLE_PRESENTATION_ATTRS)


def _is_leaf(elem: etree._Element) -> bool:
    """True when the element has no child element nodes (comments/PIs don't count)."""
    return not any(_is_element(child) for child in elem)


def _resolved_paint_prop(elem: etree._Element, decls: dict[str, str], prop: str) -> str | None:
    """Effective value of a paint property on the element itself.

    Inline `style="…"` wins over the presentation attribute (CSS specificity); no cascade or
    inheritance from ancestors is resolved — this is a per-element view.
    """
    val = decls.get(prop)
    if val is None:
        val = elem.get(prop)
    return val.strip() if val is not None else None


def _paint_of(elem: etree._Element) -> PaintInfo:
    """Per-element paint summary: effective fill / stroke / stroke-width and a stroke-only flag.

    `stroke_only` is True when the element paints a stroke (a stroke value that is not `none`) and
    explicitly sets fill to `none` — the exact "stroke-only element" shape DoD check S13 asks about.
    """
    decls = _parse_style_decls(elem.get("style") or "")
    fill = _resolved_paint_prop(elem, decls, "fill")
    stroke = _resolved_paint_prop(elem, decls, "stroke")
    stroke_width = _resolved_paint_prop(elem, decls, "stroke-width")

    has_stroke = stroke is not None and stroke.lower() != "none"
    no_fill = fill is not None and fill.lower() == "none"
    return PaintInfo(
        fill=fill,
        stroke=stroke,
        stroke_width=stroke_width,
        stroke_only=has_stroke and no_fill,
    )


def _num_attr(elem: etree._Element, name: str) -> float | None:
    """Parse a single numeric (length) attribute, stripping a trailing unit. None if absent/bad."""
    raw = elem.get(name)
    if raw is None:
        return None
    match = re.match(r"\s*([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)", raw)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:  # pragma: no cover - regex already guarantees a float-parseable token
        return None


def _bbox_of(elem: etree._Element) -> BBox | None:
    """Attribute-derived axis-aligned bbox for an element, or None when not derivable.

    Direct DOM only (ADR-005): the box comes from the element's own geometry attributes. Element
    transforms and path/polyline geometry are NOT applied — those need the render engine, so such
    elements return None rather than a wrong box. A `transform` on the element also yields None to
    avoid reporting an untransformed box as if it were final.
    """
    if elem.get("transform"):
        return None
    tag = _local_name(elem)
    if tag not in _BBOXABLE_TAGS:
        return None

    if tag in ("rect", "image", "use"):
        x = _num_attr(elem, "x") or 0.0
        y = _num_attr(elem, "y") or 0.0
        w = _num_attr(elem, "width")
        h = _num_attr(elem, "height")
        if w is None or h is None or w < 0 or h < 0:
            return None
        return BBox(x=x, y=y, width=w, height=h)
    if tag == "circle":
        cx = _num_attr(elem, "cx") or 0.0
        cy = _num_attr(elem, "cy") or 0.0
        r = _num_attr(elem, "r")
        if r is None or r < 0:
            return None
        return BBox(x=cx - r, y=cy - r, width=2 * r, height=2 * r)
    if tag == "ellipse":
        cx = _num_attr(elem, "cx") or 0.0
        cy = _num_attr(elem, "cy") or 0.0
        rx = _num_attr(elem, "rx")
        ry = _num_attr(elem, "ry")
        if rx is None or ry is None or rx < 0 or ry < 0:
            return None
        return BBox(x=cx - rx, y=cy - ry, width=2 * rx, height=2 * ry)
    if tag == "line":
        x1 = _num_attr(elem, "x1")
        y1 = _num_attr(elem, "y1")
        x2 = _num_attr(elem, "x2")
        y2 = _num_attr(elem, "y2")
        if x1 is None or y1 is None or x2 is None or y2 is None:
            return None
        lo_x, lo_y = min(x1, x2), min(y1, y2)
        return BBox(x=lo_x, y=lo_y, width=abs(x2 - x1), height=abs(y2 - y1))
    if tag in ("polygon", "polyline"):
        pts = _parse_points(elem.get("points"))
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        lo_x, lo_y = min(xs), min(ys)
        return BBox(x=lo_x, y=lo_y, width=max(xs) - lo_x, height=max(ys) - lo_y)
    return None  # pragma: no cover - all _BBOXABLE_TAGS handled above


def _parse_points(raw: str | None) -> list[tuple[float, float]]:
    """Parse a polygon/polyline `points` value into (x, y) pairs (empty list when unusable).

    Coordinates may be separated by whitespace and/or commas (SVG allows either). A trailing lone
    number with no pair is dropped; a wholly unparseable value yields an empty list.
    """
    if not raw:
        return []
    toks = [t for t in re.split(r"[,\s]+", raw.strip()) if t != ""]
    nums: list[float] = []
    for tok in toks:
        try:
            nums.append(float(tok))
        except ValueError:
            return []
    return [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]


def _parse_style_decls(style: str) -> dict[str, str]:
    """Parse an inline `style="a:b;c:d"` block into a property->value dict."""
    decls: dict[str, str] = {}
    for match in _DECL_RE.finditer(style):
        prop = match.group(1).strip().lower()
        value = match.group(2).strip()
        if prop:
            decls[prop] = value
    return decls


def _normalize_color(raw: str) -> str | None:
    """Normalize a color token; drop non-paint values (none/inherit/url refs).

    Lowercases, expands 3-digit hex to 6-digit hex. Returns None for values that are
    not actual colors (e.g. `none`, `inherit`, `currentColor`, `url(#grad)`).
    """
    value = raw.strip()
    if not value:
        return None
    low = value.lower()
    if low in ("none", "inherit", "transparent", "currentcolor", "context-fill", "context-stroke"):
        return None
    if low.startswith("url("):
        return None
    if low.startswith("#"):
        hexpart = low[1:]
        if re.fullmatch(r"[0-9a-f]{3}", hexpart):
            hexpart = "".join(ch * 2 for ch in hexpart)
        if re.fullmatch(r"[0-9a-f]{6}", hexpart) or re.fullmatch(r"[0-9a-f]{8}", hexpart):
            return f"#{hexpart}"
        return None
    # rgb()/rgba()/hsl()/named colors: keep the normalized lowercase token as-is.
    return low


def _load_tree(doc_id: str) -> tuple[DocEntry, etree._Element]:
    """Resolve `doc_id`, parse its WORKING COPY once, return (entry, root element).

    Raises `DocumentNotFound` for an unknown id and `InspectionError` for malformed /
    unsafe XML. Both carry stable, host-path-free public messages.
    """
    try:
        entry = get_registry().get(doc_id)
    except KeyError:
        raise DocumentNotFound("document id not found") from None
    try:
        tree = parse_svg_file(Path(entry.working_path))
    except UnsafeXMLError as exc:
        _logger.error("inspect parse failed", extra={"doc_id": doc_id, "detail": str(exc)})
        raise InspectionError("document could not be parsed safely") from exc
    return entry, tree.getroot()


def _iter_objects(root: etree._Element) -> list[etree._Element]:
    """Object nodes: SVG-namespace elements minus structural-only tags."""
    out: list[etree._Element] = []
    for elem in root.iter():
        if not _is_element(elem):
            continue
        if _local_name(elem) in _NON_OBJECT_TAGS:
            continue
        out.append(elem)
    return out


# --- Inspection functions ---------------------------------------------------


def inspect_summary(doc_id: str) -> DocSummary:
    """Document summary: size, units, viewBox, page count, object/layer counts."""
    _entry, root = _load_tree(doc_id)

    width = root.get("width")
    height = root.get("height")

    viewbox: list[float] | None = None
    vb_raw = root.get("viewBox")
    if vb_raw:
        parts = re.split(r"[,\s]+", vb_raw.strip())
        nums: list[float] = []
        for part in parts:
            if part == "":
                continue
            try:
                nums.append(float(part))
            except ValueError:
                nums = []
                break
        if len(nums) == 4:
            viewbox = nums

    units = root.get(f"{{{INKSCAPE_NS}}}document-units") or root.get(
        f"{{{SODIPODI_NS}}}document-units"
    )
    if units is None and width is not None:
        unit_match = re.search(r"[a-z%]+$", width.strip())
        if unit_match:
            units = unit_match.group(0)

    # Page count: <inkscape:page> elements (Inkscape 1.x multipage); default 1.
    page_count = sum(1 for elem in root.iter(f"{{{INKSCAPE_NS}}}page") if _is_element(elem))
    if page_count == 0:
        page_count = 1

    num_objects = len(_iter_objects(root))
    num_layers = sum(1 for elem in root.iter() if _is_element(elem) and _is_layer(elem))

    return DocSummary(
        doc_id=doc_id,
        width=width,
        height=height,
        units=units,
        viewbox=viewbox,
        page_count=page_count,
        num_objects=num_objects,
        num_layers=num_layers,
        root_tag=_local_name(root),
    )


def _build_node(elem: etree._Element) -> TreeNode:
    children = [_build_node(child) for child in elem if _is_element(child)]
    return TreeNode(
        tag=_local_name(elem),
        id=_id_of(elem),
        label=_label_of(elem),
        paint=_paint_of(elem),
        is_layer=_is_layer(elem),
        is_leaf=not children,
        bbox=_bbox_of(elem),
        children=children,
    )


def inspect_tree(doc_id: str) -> DocTree:
    """Full element tree (local tags + ids + inkscape:labels)."""
    _entry, root = _load_tree(doc_id)
    return DocTree(doc_id=doc_id, root=_build_node(root))


def inspect_layers(doc_id: str) -> DocLayers:
    """All Inkscape layers (`<g inkscape:groupmode="layer">`) with visible/locked state."""
    _entry, root = _load_tree(doc_id)
    layers: list[LayerInfo] = []
    for elem in root.iter():
        if not _is_element(elem) or not _is_layer(elem):
            continue
        style = elem.get("style") or ""
        decls = _parse_style_decls(style)
        display = decls.get("display")
        visibility = decls.get("visibility")
        visible = display != "none" and visibility != "hidden"
        locked = elem.get(_SODIPODI_INSENSITIVE) == "true"
        num_children = sum(1 for child in elem if _is_element(child))
        layers.append(
            LayerInfo(
                id=_id_of(elem),
                label=_label_of(elem),
                visible=visible,
                locked=locked,
                num_children=num_children,
            )
        )
    return DocLayers(doc_id=doc_id, layers=layers)


def inspect_objects(doc_id: str) -> DocObjects:
    """Flattened object list (see module docstring for the 'object' definition)."""
    _entry, root = _load_tree(doc_id)
    objects = [
        ObjectInfo(
            id=_id_of(elem),
            tag=_local_name(elem),
            label=_label_of(elem),
            has_style=_has_style(elem),
            paint=_paint_of(elem),
            is_layer=_is_layer(elem),
            is_leaf=_is_leaf(elem),
            bbox=_bbox_of(elem),
        )
        for elem in _iter_objects(root)
    ]
    return DocObjects(doc_id=doc_id, objects=objects)


def _count_css_rules(style_text: str) -> int:
    """Count CSS rule blocks (`selector { ... }`) inside a <style> body."""
    # Strip comments, then count top-level `{ ... }` blocks.
    stripped = re.sub(r"/\*.*?\*/", "", style_text, flags=re.DOTALL)
    return stripped.count("{")


# --- CSS-cascade paint resolution for find_objects matching --------
#
# Read-only refinement of the `fill` / `stroke` FILTER matching only. The reported `ObjectRef.fill`
# / `.stroke` stay the per-element authored token (back-compat); matching uses a separately-computed
# EFFECTIVE value that folds in `<style>`-rule / class / id paint and SVG inheritance.


class _CssRule:
    """One parsed `<style>` rule: a single simple selector, its declarations, and CSS specificity.

    `specificity` is the standard (id, class, type) tuple compared lexicographically so a more
    specific rule wins; rule order (source position) breaks ties and is tracked separately.
    """

    __slots__ = ("cls", "decls", "elem_id", "order", "specificity", "tag")

    def __init__(
        self,
        *,
        tag: str | None,
        cls: str | None,
        elem_id: str | None,
        decls: dict[str, str],
        order: int,
    ) -> None:
        self.tag = tag
        self.cls = cls
        self.elem_id = elem_id
        self.decls = decls
        self.order = order
        self.specificity = (
            1 if elem_id else 0,
            1 if cls else 0,
            1 if tag else 0,
        )

    def matches(self, *, tag: str, classes: frozenset[str], elem_id: str | None) -> bool:
        if self.tag is not None and self.tag != tag:
            return False
        if self.cls is not None and self.cls not in classes:
            return False
        if self.elem_id is not None and self.elem_id != elem_id:
            return False
        return True


def _parse_css_rules(style_text: str) -> list[_CssRule]:
    """Parse a `<style>` body into a flat list of :class:`_CssRule` (element / `.class` / `#id`).

    Minimal selector support per: a single type, class, or id selector, plus selector lists
    (`a, b`) and descendant/compound selectors — for which only the RIGHTMOST simple token (the
    "key" selector) is honored (its specificity is computed from that token alone). Selectors with
    a syntax we do not model are skipped rather than guessed. `@media` / `@font-face` and other
    at-rule blocks are dropped (their bodies are not paint declarations). Comments are stripped
    first. No new dependency — stdlib regex only.
    """
    stripped = re.sub(r"/\*.*?\*/", "", style_text, flags=re.DOTALL)
    rules: list[_CssRule] = []
    order = 0
    for match in _CSS_RULE_RE.finditer(stripped):
        selector_group, body = match.group(1), match.group(2)
        # `selector_group` may start with an at-rule prelude (`@media ...`); skip those wholesale —
        # we do not descend into nested blocks (the regex is non-nesting by construction).
        if "@" in selector_group:
            continue
        decls = _parse_style_decls(body)
        if not decls:
            continue
        for selector in selector_group.split(","):
            sel = selector.strip()
            if not sel:
                continue
            # Descendant/compound: match on the rightmost simple token (the key selector).
            key = sel.split()[-1]
            simple = _CSS_SIMPLE_SELECTOR_RE.match(key)
            if simple is None:
                continue
            tag, cls, elem_id = simple.group(1), simple.group(2), simple.group(3)
            if tag is None and cls is None and elem_id is None:
                # `*` universal: applies to every element (no specificity contribution).
                pass
            rules.append(_CssRule(tag=tag, cls=cls, elem_id=elem_id, decls=decls, order=order))
        order += 1
    return rules


def _document_css_rules(root: etree._Element) -> list[_CssRule]:
    """All CSS rules from every `<style>` element in the document, in source order."""
    rules: list[_CssRule] = []
    for elem in root.iter():
        if _is_element(elem) and _local_name(elem) == "style":
            rules.extend(_parse_css_rules(elem.text or ""))
    return rules


def _element_classes(elem: etree._Element) -> frozenset[str]:
    """The element's `class` attribute split into a set of class names (empty when unset)."""
    raw = elem.get("class")
    if not raw:
        return frozenset()
    return frozenset(tok for tok in raw.split() if tok)


def _own_paint_value(elem: etree._Element, rules: list[_CssRule], prop: str) -> str | None:
    """The element's OWN effective value for `prop`, applying the cascade WITHOUT inheritance.

    Cascade order (lowest → highest): matching `<style>` rules (by specificity, then source order)
    < the presentation attribute < the inline `style` declaration. Returns None when the property
    is not set on the element by any of these sources (the caller walks ancestors for inheritance).
    """
    value: str | None = None
    # `<style>` rules: take the winner by (specificity, source order).
    classes = _element_classes(elem)
    tag = _local_name(elem)
    elem_id = _id_of(elem)
    best: tuple[tuple[int, int, int], int] | None = None
    for rule in rules:
        if prop not in rule.decls:
            continue
        if not rule.matches(tag=tag, classes=classes, elem_id=elem_id):
            continue
        rank = (rule.specificity, rule.order)
        if best is None or rank > best:
            best = rank
            value = rule.decls[prop]
    # Presentation attribute beats any `<style>` rule.
    attr = elem.get(prop)
    if attr is not None:
        value = attr
    # Inline style beats the presentation attribute.
    inline = _parse_style_decls(elem.get("style") or "").get(prop)
    if inline is not None:
        value = inline
    return value.strip() if value is not None else None


def _effective_paint_for_match(
    elem: etree._Element,
    rules: list[_CssRule],
    parents: dict[etree._Element, etree._Element],
    prop: str,
) -> str | None:
    """Resolve the EFFECTIVE paint value for `prop`, folding in the cascade AND SVG inheritance.

    `fill` / `stroke` inherit, so an element with no own value (after the `<style>`/attr/inline
    cascade) takes the nearest ancestor's resolved value. `none` is a real value that stops the
    walk (it does NOT inherit further). Returns None only when no ancestor up to the root sets it.

    Used ONLY for `find_objects` filter matching — the reported `ObjectRef.fill` / `.stroke` remain
    the per-element authored token (back-compat).
    """
    current: etree._Element | None = elem
    while current is not None:
        own = _own_paint_value(current, rules, prop)
        if own is not None and own != "inherit":
            return own
        current = parents.get(current)
    return None


def _build_parent_map(root: etree._Element) -> dict[etree._Element, etree._Element]:
    """Map each element to its parent (for the memoized ancestor walk in inheritance resolution)."""
    parents: dict[etree._Element, etree._Element] = {}
    for parent in root.iter():
        if not _is_element(parent):
            continue
        for child in parent:
            if _is_element(child):
                parents[child] = parent
    return parents


def inspect_styles(doc_id: str) -> DocStyles:
    """Distinct fill/stroke colors, inline-style count, and CSS rule count."""
    _entry, root = _load_tree(doc_id)
    colors: list[str] = []
    seen: set[str] = set()
    inline_style_count = 0
    css_rule_count = 0

    def add_color(raw: str | None) -> None:
        if not raw:
            return
        norm = _normalize_color(raw)
        if norm is not None and norm not in seen:
            seen.add(norm)
            colors.append(norm)

    for elem in root.iter():
        if not _is_element(elem):
            continue
        if _local_name(elem) == "style":
            css_rule_count += _count_css_rules(elem.text or "")
            continue
        style = elem.get("style")
        if style:
            inline_style_count += 1
            decls = _parse_style_decls(style)
            add_color(decls.get("fill"))
            add_color(decls.get("stroke"))
        add_color(elem.get("fill"))
        add_color(elem.get("stroke"))

    return DocStyles(
        doc_id=doc_id,
        colors=colors,
        inline_style_count=inline_style_count,
        css_rule_count=css_rule_count,
    )


def _font_families_from_value(value: str) -> list[str]:
    """Split a font-family value into individual family names (comma list, quotes stripped)."""
    families: list[str] = []
    for part in value.split(","):
        fam = part.strip().strip("'\"").strip()
        if fam:
            families.append(fam)
    return families


def installed_font_families() -> set[str] | None:
    """Lowercased installed font-family names from `fc-list`, or None if it can't be queried.

    Shared, single-source font-availability detection: the validation engine reuses this same
    function so `validate_document` V3 (missing_font) and `inspect_document` / the fonts resource
    (`available`) never diverge. Comma-separated alias lists are split. Never raises — any failure
    (binary absent, launch error, timeout, non-zero exit) returns None so callers degrade to "not
    checked" rather than a false "missing".
    """
    binary = shutil.which("fc-list")
    if binary is None:
        return None
    try:
        result: ProcessResult = run_process([binary, ":", "family"])
    except ProcessError as exc:
        _logger.info("fc-list launch failed", extra={"detail": str(exc)})
        return None
    if result.timed_out or result.returncode != 0:
        return None

    families: set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        for alias in line.split(","):
            fam = alias.strip()
            if fam:
                families.add(fam.lower())
    return families


def _font_available(family: str, installed: set[str] | None) -> bool | None:
    """Whether a family resolves: True for generics / installed names, False if absent, None if the
    font database was unavailable (`installed is None` — the check was skipped, not "missing")."""
    low = family.strip().lower()
    if low in GENERIC_FONT_KEYWORDS:
        return True
    if installed is None:
        return None
    return low in installed


def inspect_fonts(doc_id: str) -> DocFonts:
    """Font-family values from inline styles, `<style>` bodies, and `font-family` attrs.

    Each font also carries `available` (resolved against the installed font database via the SAME
    detection `validate_document` V3 uses) and `used_by` (the id of the first referencing element).
    """
    _entry, root = _load_tree(doc_id)
    counts: dict[str, int] = {}
    used_by: dict[str, str | None] = {}
    order: list[str] = []

    def add(fam: str, elem_id: str | None) -> None:
        if fam not in counts:
            counts[fam] = 0
            order.append(fam)
            used_by[fam] = elem_id
        counts[fam] += 1

    for elem in root.iter():
        if not _is_element(elem):
            continue
        if _local_name(elem) == "style":
            for match in re.finditer(
                r"font-family\s*:\s*([^;}\n]+)", elem.text or "", flags=re.IGNORECASE
            ):
                for fam in _font_families_from_value(match.group(1)):
                    add(fam, _id_of(elem))
            continue
        style = elem.get("style")
        if style:
            decls = _parse_style_decls(style)
            fam_val = decls.get("font-family")
            if fam_val:
                for fam in _font_families_from_value(fam_val):
                    add(fam, _id_of(elem))
        attr_val = elem.get("font-family")
        if attr_val:
            for fam in _font_families_from_value(attr_val):
                add(fam, _id_of(elem))

    installed = installed_font_families() if order else None
    fonts = [
        FontInfo(
            family=fam,
            count=counts[fam],
            available=_font_available(fam, installed),
            used_by=used_by[fam],
        )
        for fam in order
    ]
    return DocFonts(doc_id=doc_id, fonts=fonts)


def _href_external(href: str) -> bool:
    """External iff not an in-document `#fragment` and not a `data:` URI."""
    h = href.strip()
    if h.startswith("#"):
        return False
    if h.lower().startswith("data:"):
        return False
    return True


def inspect_assets(doc_id: str) -> DocAssets:
    """Referenced assets: `<image>` hrefs, `<use>` refs, and external `url()` refs."""
    _entry, root = _load_tree(doc_id)
    assets: list[AssetInfo] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, href: str, used_by: str | None) -> None:
        href = href.strip()
        if not href:
            return
        key = (kind, href)
        if key in seen:
            return
        seen.add(key)
        assets.append(
            AssetInfo(kind=kind, href=href, external=_href_external(href), used_by=used_by)
        )

    for elem in root.iter():
        if not _is_element(elem):
            continue
        local = _local_name(elem)
        elem_id = _id_of(elem)
        href = elem.get(_XLINK_HREF) or elem.get("href")
        if local == "image" and href:
            add("image", href, elem_id)
        elif local == "use" and href:
            add("use", href, elem_id)
        elif href and local not in ("image", "use"):
            add("other", href, elem_id)
        # url(...) references in style / fill / stroke / mask / clip-path / filter etc.
        for attr in ("style", "fill", "stroke", "mask", "clip-path", "filter"):
            val = elem.get(attr)
            if not val:
                continue
            for match in _URL_REF_RE.finditer(val):
                ref = match.group(1)
                if _href_external(ref):
                    add("other", ref, elem_id)

    return DocAssets(doc_id=doc_id, assets=assets)


# --- Addressable object list + find_objects ------------------------


def _text_content(elem: etree._Element) -> str | None:
    """Concatenated text content of an element (own + descendant text), or None when empty.

    Joins `itertext()` and collapses runs of whitespace to single spaces so a substring search is
    stable across pretty-printed `<tspan>` markup. Returns None for a wholly empty/whitespace node.
    """
    raw = "".join(t for t in elem.itertext() if isinstance(t, str))
    collapsed = re.sub(r"\s+", " ", raw).strip()
    return collapsed or None


def _object_ref(elem: etree._Element) -> ObjectRef | None:
    """Build an :class:`ObjectRef` for an element, or None when it has no addressable `id`.

    The effective paint is read with the SAME style-over-attribute resolution as `_paint_of`
    (`PaintInfo`), so `fill` / `stroke` here match what every other inspection surface reports.
    """
    oid = _id_of(elem)
    if not oid:
        return None
    paint = _paint_of(elem)
    return ObjectRef(
        object_id=oid,
        tag=_local_name(elem),
        bbox=_bbox_of(elem),
        fill=paint.fill,
        stroke=paint.stroke,
        text=_text_content(elem),
    )


def list_objects(doc_id: str) -> list[ObjectRef]:
    """Addressable object list for a document: one :class:`ObjectRef` per id-bearing object node.

    The 'object' definition matches `inspect_objects` (SVG-namespace elements minus structural-only
    tags); elements WITHOUT an `id` are dropped because they cannot be targeted by the id-taking
    edit tools. Used both by `find_objects` (as the unfiltered universe) and additively by
    `inspect_document` so an agent that did NOT author a document can discover targetable ids.
    """
    _entry, root = _load_tree(doc_id)
    refs: list[ObjectRef] = []
    for elem in _iter_objects(root):
        ref = _object_ref(elem)
        if ref is not None:
            refs.append(ref)
    return refs


def _bbox_intersects(a: BBox, b: BBox) -> bool:
    """True when two axis-aligned boxes overlap (shared edge/corner counts as intersection)."""
    return (
        a.x <= b.x + b.width
        and b.x <= a.x + a.width
        and a.y <= b.y + b.height
        and b.y <= a.y + a.height
    )


# --- Geometry-accurate (engine) bbox for find_objects --------------
#
# Opt-in, batched, transform-/outline-aware boxes via the Inkscape render engine. A single
# `--query-all` invocation returns `id,x,y,w,h` for EVERY object, so we never spawn per object. The
# call is sandbox-safe (arg list, never a shell string; the input is the registry working copy;
# per-process timeout via run_inkscape) and degrades to None on any fault (no binary, timeout,
# non-zero exit, unparseable line) — never crashes, never leaks a host path.


def _parse_query_all(stdout: str) -> dict[str, BBox]:
    """Parse `inkscape --query-all` CSV output (`id,x,y,width,height` per line) into id -> BBox.

    Lines that are malformed (wrong field count, non-numeric, negative extent) are skipped rather
    than failing the whole batch — a single odd row never poisons the rest.
    """
    boxes: dict[str, BBox] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) != 5:
            continue
        oid = parts[0].strip()
        if not oid:
            continue
        try:
            x, y, w, h = (float(p) for p in parts[1:])
        except ValueError:
            continue
        if w < 0 or h < 0:
            continue
        boxes[oid] = BBox(x=x, y=y, width=w, height=h)
    return boxes


def _engine_bboxes(working_path: str) -> dict[str, BBox]:
    """Geometry-accurate boxes for every object via ONE `inkscape --query-all` call (or empty).

    Returns an id -> :class:`BBox` map (transform-/outline-aware, the engine's true bounds). On any
    engine fault — binary absent, launch failure, timeout, non-zero exit, no parseable rows —
    returns an empty dict so the caller degrades to the attribute box. Never raises, never leaks a
    host path.
    """
    try:
        result = run_inkscape([str(working_path), "--query-all"])
    except ProcessError as exc:
        _logger.info("query-all launch failed", extra={"detail": str(exc)})
        return {}
    if result.timed_out or result.returncode != 0:
        _logger.info(
            "query-all engine call failed",
            extra={"timed_out": result.timed_out, "returncode": result.returncode},
        )
        return {}
    return _parse_query_all(result.stdout)


def find_objects(
    doc_id: str,
    *,
    tag: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    text: str | None = None,
    id_prefix: str | None = None,
    bbox: BBox | None = None,
    accurate_bbox: bool = False,
) -> FindResult:
    """Filter a document's addressable objects by ANY combination of criteria (AND semantics).

    Every supplied filter must match for an object to be returned (an object with no `id` is never
    returned — it is not addressable). Filter semantics:

    - `tag`: exact local element-name match (case-sensitive, namespace stripped), e.g. ``"rect"``.
    - `fill` / `stroke`: paint match against the object's EFFECTIVE rendered paint, compared through
      `inkscape_mcp.edit.dom.color_key` so casing / hex shorthand differences are ignored
      (``#FFF`` matches ``#ffffff``). The effective value folds in the FULL CSS cascade:
      a matching `<style>` rule (element / `.class` / `#id` selector, by specificity then source
      order) < the presentation attribute < the inline `style`, and SVG inheritance — an element
      with no own paint takes the nearest ancestor's resolved `fill` / `stroke` (`none` is a real
      value that stops inheritance). So an object painted only via a CSS class or inherited from an
      ancestor `<g>` IS matched. NOTE: the REPORTED `ObjectRef.fill` / `.stroke` stay the
      per-element authored token (inline `style` over presentation attr; no cascade) for back-compat
      — only the FILTER uses the cascade-resolved value. Objects whose effective paint is unset
      never match.
    - `text`: case-insensitive substring match against the object's collapsed text content.
    - `id_prefix`: the object's `id` starts with this string.
    - `bbox`: keep objects whose box INTERSECTS the given box (shared edge/corner counts). By
      default this is the attribute-derived box, and objects with `bbox is None` (path / text /
      group / transformed elements — see :class:`BBox`) are EXCLUDED. When `accurate_bbox=True`, the
      geometry-accurate engine box is used instead (see below), so those objects can now match.
    - `accurate_bbox`: opt-in geometry-accurate boxes. When True, ONE batched
      ``inkscape --query-all`` call computes transform-/outline-aware boxes for every object; each
      returned `ObjectRef.bbox` (and the `bbox` filter) then uses that true box where the engine
      reported one, falling back to the attribute box otherwise. Default False keeps the cheap
      attribute-only path. If the Inkscape binary is unavailable or the call fails, this degrades
      gracefully to the attribute box — it never raises.

    Raises `DocumentNotFound` (unknown id) / `InspectionError` (unsafe XML); both carry stable,
    host-path-free messages. Read-only — no snapshot / Operation Record (ADR-004 N/A).
    """
    from inkscape_mcp.edit.dom import color_key  # local import: edit.dom imports from this module

    entry, root = _load_tree(doc_id)

    fill_key = color_key(fill) if fill is not None else None
    stroke_key = color_key(stroke) if stroke is not None else None
    text_needle = text.lower() if text is not None else None

    # CSS-cascade paint resolution — built once per call; only needed when a paint filter
    # is supplied (the reported tokens stay per-element, so no work otherwise).
    css_rules: list[_CssRule] = []
    parents: dict[etree._Element, etree._Element] = {}
    if fill_key is not None or stroke_key is not None:
        css_rules = _document_css_rules(root)
        parents = _build_parent_map(root)

    # Geometry-accurate boxes — one batched engine call, only when opted in. An empty map
    # (binary absent / engine fault) transparently falls back to the attribute box per element.
    engine_boxes = _engine_bboxes(entry.working_path) if accurate_bbox else {}

    matched: list[ObjectRef] = []
    for elem in _iter_objects(root):
        ref = _object_ref(elem)
        if ref is None:
            continue
        if accurate_bbox:
            engine_box = engine_boxes.get(ref.object_id)
            if engine_box is not None:
                ref.bbox = engine_box

        if tag is not None and ref.tag != tag:
            continue
        if fill_key is not None:
            eff = _effective_paint_for_match(elem, css_rules, parents, "fill")
            if eff is None or color_key(eff) != fill_key:
                continue
        if stroke_key is not None:
            eff = _effective_paint_for_match(elem, css_rules, parents, "stroke")
            if eff is None or color_key(eff) != stroke_key:
                continue
        if text_needle is not None and (ref.text is None or text_needle not in ref.text.lower()):
            continue
        if id_prefix is not None and not ref.object_id.startswith(id_prefix):
            continue
        if bbox is not None and (ref.bbox is None or not _bbox_intersects(ref.bbox, bbox)):
            continue
        matched.append(ref)

    return FindResult(doc_id=doc_id, objects=matched, count=len(matched))
