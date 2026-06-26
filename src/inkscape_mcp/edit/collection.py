"""Cross-document / collection layer engine (E16-05).

Pure, framework-free helpers behind the SMALL TYPED collection tools (ADR-002 — NOT a portmanteau):

- :func:`make_compose_grid` builds the reversible ``mutate`` closure that lays N ALREADY-RESOLVED
  source subtrees out in a ``rows`` x ``cols`` grid in a target document, each asset deep-copied
  (re-id'd, intra-clone refs rewritten) and wrapped in a ``<g>`` cell group with a translate +
  optional scale-to-fit transform. It reuses the EXACT placement primitives ``tile`` uses
  (:func:`dom.deep_copy_with_new_ids`, :func:`dom.prepend_transform`, :func:`dom.fmt_num`) — it adds
  only the *different-asset-per-cell* + optional scale-to-fit layout on top. Direct DOM (ADR-005);
  no Inkscape engine. The tool layer routes it through ``apply_edit`` so the whole sheet lands under
  one snapshot + one Operation Record (ADR-004).

- The cross-doc CONSISTENCY-VERDICT machinery (:func:`build_consistency_verdict` and the
  per-property extractors) that the ``*_set`` tools layer over their single-doc engines. The verdict
  is STRUCTURED (per-property agree/disagree + the differing values + which ``doc_ids`` differ), not
  prose: it audits agreement across a set on ``viewBox`` (canvas box), ``stroke_width`` (the
  dominant stroke-width convention), and ``id_naming`` (the dominant id-naming style — kebab / snake
  / camel / …). Read-only; no mutation.

SECURITY (sec.12): every value surfaced in a verdict is a document-derived primitive (numbers,
short style tokens) — never a host path. Source-subtree extraction is bounded by the same
``MAX_CELLS`` ceiling that bounds the clone count, so a pathological grid can never exhaust memory.
"""

from __future__ import annotations

import math
import re

from lxml import etree
from pydantic import BaseModel

from inkscape_mcp.edit.dom import (
    EditError,
    all_ids,
    deep_copy_with_new_ids,
    fmt_num,
    parse_viewbox,
    prepend_transform,
)
from inkscape_mcp.edit.pipeline import MutateFn

__all__ = [
    "MAX_CELLS",
    "ConsistencyProperty",
    "ConsistencySignals",
    "ConsistencyVerdict",
    "GridCell",
    "build_consistency_verdict",
    "consistency_signals",
    "dominant_id_naming",
    "dominant_stroke_width",
    "id_naming_style",
    "make_compose_grid",
    "make_place_document",
    "viewbox_signature",
]

_SVG = "{http://www.w3.org/2000/svg}"

#: Hard ceiling on grid cells (``rows * cols``) — mirrors ``transform.MAX_TILES``. Bounds the clone
#: count and the output size so a pathological sheet can never exhaust memory (sec.12).
MAX_CELLS = 1024


class GridCell(BaseModel):
    """One placed asset in a composed grid: its grid coordinates and the new top-level group id.

    ``source`` is a short, host-path-free label for what was placed (the source ``doc_id`` or the
    source ``object_id``). ``group_id`` is the id of the ``<g>`` cell wrapper inserted into the
    target document. ``row`` / ``col`` are zero-based grid coordinates (row-major).
    """

    row: int
    col: int
    group_id: str
    source: str


def _require_finite(name: str, value: float) -> None:
    """Reject a non-finite numeric input with a stable, host-path-free :class:`EditError`."""
    if not math.isfinite(value):
        raise EditError(f"{name} must be finite")


def _content_bbox(elem: etree._Element) -> tuple[float, float, float, float] | None:
    """Best-effort axis-aligned bbox of a subtree from its OWN geometry attributes (no engine).

    Walks the element + descendants and unions the naive bounding boxes of the primitive shapes it
    can read directly from attributes (``rect``/``image``/``use`` x/y/width/height, ``circle`` /
    ``ellipse`` centre+radius, ``line`` endpoints). Transforms on descendants are NOT applied (a
    direct-DOM heuristic, ADR-005 — exact geometry needs the engine), so this is used ONLY to derive
    a scale-to-fit factor; it returns ``None`` when nothing measurable is found (then no scaling is
    applied). Bounds are finite-checked.
    """
    xs: list[float] = []
    ys: list[float] = []

    def _add_box(x: float, y: float, w: float, h: float) -> None:
        if all(math.isfinite(v) for v in (x, y, w, h)) and w >= 0 and h >= 0:
            xs.extend((x, x + w))
            ys.extend((y, y + h))

    for node in elem.iter():
        if not isinstance(node.tag, str):
            continue
        local = etree.QName(node.tag).localname
        try:
            if local in ("rect", "image", "use"):
                _add_box(
                    _num(node.get("x")),
                    _num(node.get("y")),
                    _num(node.get("width")),
                    _num(node.get("height")),
                )
            elif local == "circle":
                cx, cy, r = _num(node.get("cx")), _num(node.get("cy")), _num(node.get("r"))
                _add_box(cx - r, cy - r, 2 * r, 2 * r)
            elif local == "ellipse":
                cx, cy = _num(node.get("cx")), _num(node.get("cy"))
                rx, ry = _num(node.get("rx")), _num(node.get("ry"))
                _add_box(cx - rx, cy - ry, 2 * rx, 2 * ry)
            elif local == "line":
                x1, y1 = _num(node.get("x1")), _num(node.get("y1"))
                x2, y2 = _num(node.get("x2")), _num(node.get("y2"))
                _add_box(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
        except (TypeError, ValueError):
            continue

    if not xs or not ys:
        return None
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        return None
    return x0, y0, x1 - x0, y1 - y0


def _num(raw: str | None) -> float:
    """Parse an attribute into a float (``None``/blank is 0.0); raises ``ValueError`` on garbage."""
    if raw is None:
        return 0.0
    text = raw.strip()
    if not text:
        return 0.0
    return float(text)


def make_compose_grid(
    cells: list[tuple[etree._Element, str]],
    rows: int,
    cols: int,
    cell: float,
    *,
    gap: float = 0.0,
    scale_to_fit: bool = True,
    padding: float = 0.0,
) -> tuple[MutateFn, list[GridCell]]:
    """Build a ``mutate`` closure that lays N DIFFERENT assets out in a ``rows`` x ``cols`` grid.

    ``cells`` is the ALREADY-RESOLVED, ordered list of ``(source_element, source_label)`` pairs —
    the tool layer extracts each source subtree (possibly from a DIFFERENT document) and the engine
    only places copies, so no per-asset loop or lxml subtree extract is needed by the caller. Each
    source is deep-copied (every id re-minted, intra-clone refs rewritten via
    :func:`dom.deep_copy_with_new_ids`), wrapped in a ``<g>`` whose transform translates it to its
    cell origin ``(col*(cell+gap)+padding, row*(cell+gap)+padding)`` and, when ``scale_to_fit`` and
    a measurable content bbox exist, uniformly scales it to fit ``cell - 2*padding`` (never up past
    1.0 by default — assets larger than the cell are shrunk, smaller ones left at native size).

    The grid fills ROW-MAJOR; supplying fewer than ``rows*cols`` assets simply leaves trailing cells
    empty (a contact sheet of N≤rows*cols assets). Bounds (sec.12): ``rows``/``cols`` ≥ 1 and their
    product ≤ :data:`MAX_CELLS`; ``cell`` > 0; ``gap``/``padding`` ≥ 0 and finite; the asset count
    must not exceed the cell count. Raises :class:`EditError` on any violation BEFORE the closure is
    handed to the pipeline, so a bad request never reaches a snapshot.

    Returns ``(mutate, planned_cells)`` where ``planned_cells`` is the ordered :class:`GridCell`
    placement plan (its ``group_id`` values are filled in when ``mutate`` runs, since unique-id
    minting needs the live target tree).
    """
    if rows < 1 or cols < 1:
        raise EditError("compose_grid rows and cols must each be >= 1")
    if rows * cols > MAX_CELLS:
        raise EditError(f"compose_grid too large: {rows}x{cols} exceeds {MAX_CELLS} cells")
    if not (math.isfinite(cell) and cell > 0):
        raise EditError("compose_grid cell must be a finite number greater than 0")
    _require_finite("gap", gap)
    _require_finite("padding", padding)
    if gap < 0 or padding < 0:
        raise EditError("compose_grid gap and padding must be >= 0")
    if not cells:
        raise EditError("compose_grid requires at least one asset")
    if len(cells) > rows * cols:
        raise EditError(
            f"compose_grid has {len(cells)} assets but only {rows * cols} cells ({rows}x{cols})"
        )

    inner = cell - 2 * padding
    if inner <= 0:
        raise EditError("compose_grid padding leaves no room in the cell (2*padding >= cell)")

    plan: list[GridCell] = [
        GridCell(row=i // cols, col=i % cols, group_id="", source=label)
        for i, (_elem, label) in enumerate(cells)
    ]

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        stride = cell + gap
        for entry, (source, _label) in zip(plan, cells, strict=True):
            clone, top_id = deep_copy_with_new_ids(source, root, None)
            wrapper = etree.SubElement(root, f"{_SVG}g")
            group_id = f"cell-{entry.row}-{entry.col}-{top_id}"
            wrapper.set("id", group_id)
            wrapper.append(clone)
            entry.group_id = group_id

            origin_x = entry.col * stride + padding
            origin_y = entry.row * stride + padding

            # Optional uniform scale-to-fit, applied INSIDE the translate (so the asset scales about
            # the cell origin, then is moved into place). Downscale only by default.
            if scale_to_fit:
                bbox = _content_bbox(clone)
                if bbox is not None:
                    _x, _y, bw, bh = bbox
                    longest = max(bw, bh)
                    if longest > 0:
                        factor = inner / longest
                        if factor < 1.0:
                            prepend_transform(wrapper, f"scale({fmt_num(factor)})")
            prepend_transform(wrapper, f"translate({fmt_num(origin_x)},{fmt_num(origin_y)})")

        return f"composed {len(plan)} asset(s) into a {rows}x{cols} grid"

    return mutate, plan


def make_place_document(
    source: etree._Element,
    label: str,
    x: float,
    y: float,
    scale: float = 1.0,
    *,
    result_holder: list[str] | None = None,
) -> MutateFn:
    """Build a ``mutate`` closure that PLACES one already-resolved source subtree into a target doc.

    The single-asset counterpart of :func:`make_compose_grid` (E16-10a): ``source`` is one resolved
    element — a whole source document's ROOT or a named object, possibly from a DIFFERENT document —
    deep-copied (every id re-minted, intra-clone refs rewritten via
    :func:`dom.deep_copy_with_new_ids`) and wrapped in a ``<g>`` whose transform translates it to
    ``(x, y)`` and uniformly scales it by ``scale`` about that origin. Lets existing geometry be
    re-composed cross-doc without re-authoring or an lxml subtree extract by the caller. Direct DOM
    (ADR-005); the tool layer routes it through ``apply_edit`` for a snapshot + Operation Record.

    Bounds (sec.12): ``x`` / ``y`` finite; ``scale`` finite and > 0. Raises :class:`EditError` on
    any violation BEFORE the closure runs, so a bad request never reaches a snapshot. When
    ``result_holder`` is supplied the new wrapper-group id is written into it
    (``result_holder[:] = [group_id]``) so the tool layer returns it without re-inspecting the tree.
    """
    _require_finite("x", x)
    _require_finite("y", y)
    if not (math.isfinite(scale) and scale > 0):
        raise EditError("place_document scale must be a finite number greater than 0")

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        clone, top_id = deep_copy_with_new_ids(source, root, None)
        wrapper = etree.SubElement(root, f"{_SVG}g")
        # deep_copy minted top_id unique, but the wrapper id must ALSO be unique vs. the live tree.
        existing = all_ids(root)
        base = f"placed-{top_id}"
        group_id = base
        n = 1
        while group_id in existing:
            group_id = f"{base}-{n}"
            n += 1
        wrapper.set("id", group_id)
        wrapper.append(clone)
        if scale != 1.0:
            prepend_transform(wrapper, f"scale({fmt_num(scale)})")
        prepend_transform(wrapper, f"translate({fmt_num(x)},{fmt_num(y)})")
        if result_holder is not None:
            result_holder[:] = [group_id]
        return (
            f"placed {label!r} as {group_id!r} at "
            f"({fmt_num(x)},{fmt_num(y)}) scale {fmt_num(scale)}"
        )

    return mutate


# --- Cross-document consistency verdict -------------------------------------


class ConsistencyProperty(BaseModel):
    """Agreement of ONE property across a document set (E16-05).

    ``agree`` is True iff every document in the set reports the SAME value for this property
    (documents that could not report a value are excluded from the comparison and listed in
    ``unknown_doc_ids``). ``values`` maps each distinct value (rendered as a short, host-path-free
    string) to the ``doc_ids`` that carry it, so a disagreement names exactly which docs differ.
    ``majority`` is the value carried by the most docs (the convention to converge on), or ``None``
    when there is no data.
    """

    property: str
    agree: bool
    majority: str | None
    values: dict[str, list[str]]
    unknown_doc_ids: list[str]


class ConsistencyVerdict(BaseModel):
    """Structured cross-document consistency audit over a set (E16-05).

    One :class:`ConsistencyProperty` per audited property (``viewBox``, ``stroke_width``,
    ``id_naming``). ``consistent`` is True iff EVERY audited property agrees across the set. Not
    prose: an agent reads ``properties`` to see precisely which property disagrees and which
    ``doc_ids`` carry which value.
    """

    consistent: bool
    properties: list[ConsistencyProperty]


def _bucket(values: dict[str, str]) -> tuple[dict[str, list[str]], list[str]]:
    """Group ``{doc_id: value}`` into ``{value: [doc_ids]}`` plus the docs with no value (``None``).

    A document whose value is an empty string or ``None`` is treated as UNKNOWN (excluded from the
    agreement comparison) rather than as a distinct value, so a doc that simply lacks the property
    does not spuriously flag a disagreement.
    """
    buckets: dict[str, list[str]] = {}
    unknown: list[str] = []
    for doc_id, value in values.items():
        if not value:
            unknown.append(doc_id)
            continue
        buckets.setdefault(value, []).append(doc_id)
    return buckets, unknown


def _property(name: str, values: dict[str, str]) -> ConsistencyProperty:
    """Build one :class:`ConsistencyProperty` from a ``{doc_id: value}`` map."""
    buckets, unknown = _bucket(values)
    majority = max(buckets, key=lambda v: len(buckets[v])) if buckets else None
    return ConsistencyProperty(
        property=name,
        agree=len(buckets) <= 1,
        majority=majority,
        values=buckets,
        unknown_doc_ids=unknown,
    )


def viewbox_signature(width: float | None, height: float | None) -> str:
    """Canvas-size signature ``"WxH"`` from a viewBox/canvas width+height, or ``""`` if unknown."""
    if width is None or height is None:
        return ""
    return f"{fmt_num(float(width))}x{fmt_num(float(height))}"


_STROKE_WIDTH_STYLE_RE = re.compile(r"stroke-width\s*:\s*([^;]+)")


def dominant_stroke_width(root: etree._Element) -> str:
    """The most common ``stroke-width`` value in a document (attribute or inline style), or ``""``.

    Counts every ``stroke-width`` set as a presentation attribute OR inside a ``style`` declaration
    and returns the most frequent token (ties broken by first-seen). A document with no stroke-width
    yields ``""`` (UNKNOWN — excluded from the agreement comparison). The token is a short,
    validated numeric/length string straight from the document, never a host path.
    """
    counts: dict[str, int] = {}
    order: list[str] = []
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        widths: list[str] = []
        attr = node.get("stroke-width")
        if attr and attr.strip():
            widths.append(attr.strip())
        style = node.get("style")
        if style:
            for match in _STROKE_WIDTH_STYLE_RE.finditer(style):
                token = match.group(1).strip()
                if token:
                    widths.append(token)
        for token in widths:
            if token not in counts:
                order.append(token)
            counts[token] = counts.get(token, 0) + 1
    if not counts:
        return ""
    return max(order, key=lambda t: counts[t])


_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")
_SNAKE_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)+$")
_CAMEL_RE = re.compile(r"^[a-z]+(?:[A-Z][a-z0-9]*)+$")
_PASCAL_RE = re.compile(r"^(?:[A-Z][a-z0-9]*){2,}$")


def id_naming_style(identifier: str) -> str:
    """Classify ONE id into a coarse naming style: kebab / snake / camel / pascal / flat / other."""
    if _KEBAB_RE.match(identifier):
        return "kebab"
    if _SNAKE_RE.match(identifier):
        return "snake"
    if _CAMEL_RE.match(identifier):
        return "camel"
    if _PASCAL_RE.match(identifier):
        return "pascal"
    if re.fullmatch(r"[a-z0-9]+", identifier):
        return "flat"
    return "other"


def dominant_id_naming(root: etree._Element) -> str:
    """The dominant id-naming STYLE across a document's ``id`` attributes, or ``""`` if none.

    Each ``id`` is classified by :func:`id_naming_style`; the most frequent style is returned.
    Auto-generated/structural ids count too — the intent is to surface whether a SET converges on
    one naming convention (a design-system smell when it does not). Returns ``""`` when the document
    has no ids (UNKNOWN — excluded from the comparison).
    """
    counts: dict[str, int] = {}
    order: list[str] = []
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        identifier = node.get("id")
        if not identifier:
            continue
        style = id_naming_style(identifier)
        if style not in counts:
            order.append(style)
        counts[style] = counts.get(style, 0) + 1
    if not counts:
        return ""
    return max(order, key=lambda s: counts[s])


class ConsistencySignals(BaseModel):
    """The three per-document consistency signals extracted from one parsed working copy.

    ``""`` means UNKNOWN for that signal (the document does not carry it) and is excluded from the
    cross-doc agreement comparison. All three are short, host-path-free tokens.
    """

    viewbox: str
    stroke_width: str
    id_naming: str


def consistency_signals(root: etree._Element) -> ConsistencySignals:
    """Extract a document's ``viewBox`` / ``stroke_width`` / ``id_naming`` signals in one pass.

    The viewBox signature is read from the root ``viewBox`` (falling back to root
    ``width``/``height``
    when absent); stroke-width and id-naming are the dominant values across the tree. Pure read; no
    mutation, no engine.
    """
    nums = parse_viewbox(root.get("viewBox"))
    if nums is not None and len(nums) == 4:
        viewbox = viewbox_signature(nums[2], nums[3])
    else:
        viewbox = viewbox_signature(_dimension(root.get("width")), _dimension(root.get("height")))
    return ConsistencySignals(
        viewbox=viewbox,
        stroke_width=dominant_stroke_width(root),
        id_naming=dominant_id_naming(root),
    )


def _dimension(raw: str | None) -> float | None:
    """Parse a root ``width``/``height`` into a finite float (unit suffix stripped), or ``None``."""
    if raw is None:
        return None
    text = raw.strip()
    for unit in ("px", "pt", "pc", "mm", "cm", "in", "em", "ex", "rem", "%"):
        if text.endswith(unit):
            text = text[: -len(unit)]
            break
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def build_consistency_verdict(
    viewboxes: dict[str, str],
    stroke_widths: dict[str, str],
    id_namings: dict[str, str],
) -> ConsistencyVerdict:
    """Assemble the cross-doc :class:`ConsistencyVerdict` from per-property ``{doc_id: value}``.

    Each input maps a ``doc_id`` to that document's value for the property (``""`` for unknown). The
    verdict is ``consistent`` iff every audited property agrees. The three maps are produced by the
    ``*_set`` tool layer from data it already has (viewBox from the per-doc ``QualityReport`` /
    render, stroke-width + id-naming from a single safe parse of each working copy).
    """
    properties = [
        _property("viewBox", viewboxes),
        _property("stroke_width", stroke_widths),
        _property("id_naming", id_namings),
    ]
    return ConsistencyVerdict(
        consistent=all(p.agree for p in properties),
        properties=properties,
    )
