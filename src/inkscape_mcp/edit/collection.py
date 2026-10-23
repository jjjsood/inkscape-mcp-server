"""Cross-document / collection layer engine.

Pure, framework-free helpers behind the SMALL TYPED collection tools (ADR-002 — NOT a portmanteau):

- :func:`compose_grid` lays N ALREADY-RESOLVED
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

import copy
import hashlib
import json
import math
import re
from pathlib import Path

from fastmcp.exceptions import ToolError
from lxml import etree
from pydantic import BaseModel

from inkscape_mcp.document.inspect import DocumentNotFound, InspectionError, _load_tree
from inkscape_mcp.edit.dom import (
    _CSS_CLASS_RE,
    _CSS_ID_RE,
    _CSS_RULE_RE,
    EditError,
    all_ids,
    content_bbox,
    deep_copy_with_new_ids,
    find_by_id,
    fmt_num,
    parse_viewbox,
    prepend_transform,
    require_finite,
)
from inkscape_mcp.registry import doc_not_found_message, get_registry
from inkscape_mcp.workspace import sandbox

__all__ = [
    "ConsistencyProperty",
    "ConsistencySignals",
    "ConsistencyVerdict",
    "build_consistency_verdict",
    "compose_grid",
    "consistency_signals",
    "dominant_id_naming",
    "dominant_stroke_width",
    "place_document",
    "record_grid_sources",
    "resolve_grid_assets",
    "source_fingerprint",
    "stale_copy_warnings",
    "stale_grid_sources",
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


def _num(raw: str | None) -> float:
    """Parse an attribute into a float (``None``/blank is 0.0); raises ``ValueError`` on garbage."""
    if raw is None:
        return 0.0
    text = raw.strip()
    if not text:
        return 0.0
    return float(text)


def compose_grid(
    tree: etree._ElementTree,
    cells: list[tuple[etree._Element, str]],
    rows: int,
    cols: int,
    cell: float,
    *,
    gap: float = 0.0,
    scale_to_fit: bool = True,
    padding: float = 0.0,
) -> tuple[str, list[GridCell]]:
    """Lay N DIFFERENT assets out in a ``rows`` x ``cols`` grid in ``tree``.

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
    must not exceed the cell count. Raises :class:`EditError` on any violation BEFORE the tree is
    touched, so a bad request never reaches a snapshot.

    Returns ``(summary, placed_cells)`` where ``placed_cells`` is the ordered :class:`GridCell`
    placement plan with each cell's new ``group_id`` filled in.
    """
    if rows < 1 or cols < 1:
        raise EditError("grid rows and cols must each be >= 1")
    if rows * cols > MAX_CELLS:
        raise EditError(f"grid too large: {rows}x{cols} exceeds {MAX_CELLS} cells")
    if not (math.isfinite(cell) and cell > 0):
        raise EditError("grid cell must be a finite number greater than 0")
    require_finite("gap", gap)
    require_finite("padding", padding)
    if gap < 0 or padding < 0:
        raise EditError("grid gap and padding must be >= 0")
    if not cells:
        raise EditError("grid requires at least one asset")
    if len(cells) > rows * cols:
        raise EditError(
            f"grid has {len(cells)} assets but only {rows * cols} cells ({rows}x{cols})"
        )

    inner = cell - 2 * padding
    if inner <= 0:
        raise EditError("grid padding leaves no room in the cell (2*padding >= cell)")

    plan: list[GridCell] = [
        GridCell(row=i // cols, col=i % cols, group_id="", source=label)
        for i, (_elem, label) in enumerate(cells)
    ]

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
            bbox = content_bbox(clone)
            if bbox is not None:
                _x, _y, bw, bh = bbox
                longest = max(bw, bh)
                if longest > 0:
                    factor = inner / longest
                    if factor < 1.0:
                        prepend_transform(wrapper, f"scale({fmt_num(factor)})")
        prepend_transform(wrapper, f"translate({fmt_num(origin_x)},{fmt_num(origin_y)})")

    return (
        f"composed {len(plan)} asset(s) into a {rows}x{cols} grid (copies: rebuild after "
        "editing a source)"
    ), plan


def _carry_css_rules(source: etree._Element, source_root: etree._Element) -> etree._Element:
    """Return ``source`` with the source document's CSS rules it depends on bundled in.

    A single object placed out of a document would otherwise lose the ``<style>`` rules its
    ``class`` / ``#id`` selectors point at. The rules whose class or id selectors match the object's
    subtree are copied into a synthetic ``<g>`` next to a copy of the object, so
    :func:`dom.deep_copy_with_new_ids` scopes them to the placed copy like a whole-document place.
    ``source`` is returned unchanged when it is the root or no rule applies.
    """
    if source is source_root:
        return source
    nodes = [n for n in source.iter() if isinstance(n.tag, str)]
    classes = {c for n in nodes for c in (n.get("class") or "").split()}
    ids = {n.get("id") for n in nodes}
    rules = [
        m[0].strip()
        for st in source_root.iter(f"{_SVG}style")
        for m in _CSS_RULE_RE.finditer(st.text or "")
        if classes.intersection(_CSS_CLASS_RE.findall(m[1]))
        or ids.intersection(_CSS_ID_RE.findall(m[1]))
    ]
    if not rules:
        return source
    holder = etree.Element(f"{_SVG}g")
    etree.SubElement(holder, f"{_SVG}style").text = "\n".join(rules)
    holder.append(copy.deepcopy(source))
    return holder


def place_document(
    tree: etree._ElementTree,
    source: etree._Element,
    label: str,
    x: float,
    y: float,
    scale: float = 1.0,
    *,
    source_root: etree._Element | None = None,
) -> tuple[str, str]:
    """PLACE one already-resolved source subtree into ``tree``; returns ``(summary, group_id)``.

    The single-asset counterpart of :func:`compose_grid`: ``source`` is one resolved
    element — a whole source document's ROOT or a named object, possibly from a DIFFERENT document —
    deep-copied (every id re-minted, intra-clone refs rewritten via
    :func:`dom.deep_copy_with_new_ids`) and wrapped in a ``<g>`` whose transform translates it to
    ``(x, y)`` and uniformly scales it by ``scale`` about that origin. Lets existing geometry be
    re-composed cross-doc without re-authoring or an lxml subtree extract by the caller. Direct DOM
    (ADR-005); the tool layer routes it through ``apply_edit`` for a snapshot + Operation Record.

    Bounds (sec.12): ``x`` / ``y`` finite; ``scale`` finite and > 0. Raises :class:`EditError` on
    any violation BEFORE the tree is touched, so a bad request never reaches a snapshot. The new
    wrapper-group id is returned so the tool layer need not re-inspect the tree.
    """
    require_finite("x", x)
    require_finite("y", y)
    if not (math.isfinite(scale) and scale > 0):
        raise EditError("place scale must be a finite number greater than 0")

    root = tree.getroot()
    if source_root is not None:
        source = _carry_css_rules(source, source_root)
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
    summary = (
        f"placed {label!r} as {group_id!r} at ({fmt_num(x)},{fmt_num(y)}) scale {fmt_num(scale)}"
    )
    return summary, group_id


# --- Cross-document consistency verdict -------------------------------------


class ConsistencyProperty(BaseModel):
    """Agreement of ONE property across a document set.

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
    """Structured cross-document consistency audit over a set.

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


def source_fingerprint(doc_id: str) -> str | None:
    """Short sha256 of a document's working-copy bytes, or `None` if it cannot be read."""
    try:
        data = Path(get_registry().get(doc_id).working_path).read_bytes()
    except (KeyError, OSError):
        return None
    return hashlib.sha256(data).hexdigest()[:16]


def _sources_file(doc_id: str) -> Path:
    return sandbox.doc_dir(Path(get_registry().get(doc_id).root), doc_id) / "copies.json"


def record_grid_sources(doc_id: str, copies: dict[str, list[str]]) -> None:
    """Remember each grid/place copy `{group_id: [source_doc_id, fingerprint]}` beside the sheet."""
    path = _sources_file(doc_id)
    old = json.loads(path.read_text()) if path.is_file() else {}
    path.write_text(json.dumps({**old, **copies}))


def stale_grid_sources(doc_id: str) -> list[str]:
    """Source doc_ids this sheet still holds a copy of (group present) that no longer matches."""
    try:
        path = _sources_file(doc_id)
        if not path.is_file():
            return []
        ids = all_ids(_load_tree(doc_id)[1])
    except (KeyError, DocumentNotFound, InspectionError):
        return []
    return sorted(
        {
            src
            for gid, (src, fp) in json.loads(path.read_text()).items()
            if gid in ids and source_fingerprint(src) != fp
        }
    )


def stale_copy_warnings(source_id: str) -> list[str]:
    """One warning per other document whose copy of `source_id` is now outdated."""
    return [
        f"{d.doc_id} holds a copy of {source_id} made before this edit; that copy is now stale. "
        f"Rebuild it (grid/place on a fresh canvas, or restore {d.doc_id}'s pre-copy snapshot "
        "and re-run) instead of patching the copy"
        for d in get_registry().list_documents()
        if d.doc_id != source_id and source_id in stale_grid_sources(d.doc_id)
    ]


def resolve_grid_assets(
    *,
    doc_ids: list[str] | None,
    source_ids: list[str] | None,
    source_doc_id: str | None,
) -> list[tuple[etree._Element, str]]:
    """Resolve the ordered `(source_element, label)` pairs for the `grid` op from one input mode.

    EXACTLY ONE source mode is allowed (ADR-002 — a small, unambiguous tool, not a dispatcher):

    - `doc_ids`: each whole document's ROOT element is the asset (one cell per document). The root
      is grafted as-is; the placement engine deep-copies + re-ids it, so the source working copies
      are never mutated.
    - `source_ids` (+ `source_doc_id`): each named object in the ONE `source_doc_id` is an asset.

    Reads each source through `_load_tree` (safe parse). Raises `ToolError` with a stable,
    host-path-free message for a bad mode combination, an unknown id, or a missing object.
    """
    has_docs = bool(doc_ids)
    has_objects = bool(source_ids)
    if has_docs == has_objects:
        raise ToolError("grid requires EXACTLY ONE of doc_ids OR source_ids (with source_doc_id)")

    pairs: list[tuple[etree._Element, str]] = []
    if has_docs:
        if source_doc_id is not None:
            raise ToolError("source_doc_id is only valid with source_ids, not doc_ids")
        for did in doc_ids or []:
            try:
                _entry, root = _load_tree(did)
            except DocumentNotFound as exc:
                raise ToolError(doc_not_found_message()) from exc
            except InspectionError as exc:
                raise ToolError("document could not be parsed safely") from exc
            pairs.append((root, did))
        return pairs

    if source_doc_id is None:
        raise ToolError("source_ids requires source_doc_id (the document the objects live in)")
    try:
        _entry, root = _load_tree(source_doc_id)
    except DocumentNotFound as exc:
        raise ToolError(doc_not_found_message()) from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc
    for oid in source_ids or []:
        elem = find_by_id(root, oid)
        if elem is None:
            raise ToolError("object id not found in document")
        pairs.append((elem, oid))
    return pairs
