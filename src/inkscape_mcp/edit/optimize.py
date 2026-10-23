"""Web-optimization edit engine (ADR-005 direct DOM).

Pure ``optimize_web(tree, ...) -> str`` edit for the one tool that genuinely mutates the document,
``optimize_web``. It edits the parsed working tree IN MEMORY and returns a short human summary,
so it plugs straight into :func:`inkscape_mcp.edit.pipeline.apply_edit` (snapshot + Operation
Record + before/after preview — reversible, medium risk). No MCP decorators here; the
``edit`` tool (``inkscape_mcp.tools.edit``) wraps this and maps exceptions to ``ToolError``.

Three cleanups, applied in order over the WORKING COPY only (the original is never touched):

1. **Strip editor cruft** — remove Inkscape/sodipodi editor-only elements (``sodipodi:namedview``,
   ``metadata``), every attribute in the Inkscape/sodipodi namespaces, and XML comments, then drop
   the now-unused namespace declarations (``etree.cleanup_namespaces``).
2. **Drop dead structure** — remove unreferenced ``<defs>`` children (dead templates), then every
   ``id`` attribute nothing references, then empty ``<g>``/``<defs>`` containers. The set of
   referenced ids is computed FIRST (``#frag`` / ``url(#frag)`` / ``href="#frag"`` — mirroring the
 validate + reference-rewrite discipline) so a referenced id is never stripped and no
   dangling reference is ever created.
3. **Reduce coordinate precision** — round the numbers inside geometry/coordinate attributes (path
   ``d``, ``points``, transforms, ``x``/``y``/``width``/...) to a bounded number of decimals. Only
   decimal numbers are touched; ids, hrefs, ``url(#...)`` refs, the root ``viewBox``,
   and integer flags are left exactly as-is, so geometry-precision reduction can never rewrite a
   reference.

SAFETY (sec.12): the working tree is already parsed through the normative safe parser by the
pipeline (no entities / DTD / network). This module mutates that in-memory tree only; the sole
client input that reaches it is the bounded integer ``precision`` (validated to ``[0, 8]``). It
never builds an argv, opens a path, or reaches the network.

The read-only :func:`analyze_optimizations` runs the SAME detection logic without mutating, so
``quality_report`` can surface exactly the opportunities this tool would remove.
"""

from __future__ import annotations

import re

from lxml import etree
from pydantic import BaseModel

from inkscape_mcp.document.inspect import INKSCAPE_NS, SODIPODI_NS, is_element, local_name
from inkscape_mcp.edit.dom import EditError

#: Editor-only namespaces whose attributes are stripped wholesale (editor metadata, not rendering).
_EDITOR_NS = frozenset({INKSCAPE_NS, SODIPODI_NS})

#: Inkscape attributes that carry document STRUCTURE (layers and their names), not editor state.
#: Kept unless ``aggressive``: an agent keeps editing after optimizing, and layers cost a few bytes.
_LAYER_ATTRS = frozenset(
    {
        f"{{{INKSCAPE_NS}}}groupmode",
        f"{{{INKSCAPE_NS}}}label",
        f"{{{SODIPODI_NS}}}insensitive",
        f"{{{INKSCAPE_NS}}}current-layer",
    }
)

#: Kept (with only its ``current-layer``) unless ``aggressive``, so new objects still land in the
#: layer the document was being edited in.
_NAMEDVIEW = "namedview"

#: Editor-only elements removed entirely: the sodipodi namedview and the (RDF) metadata block.
_EDITOR_ELEMENT_LOCALS = frozenset({"namedview", "metadata"})

#: Container locals that are pruned when empty (no element children).
_EMPTY_CONTAINER_LOCALS = frozenset({"g", "defs"})

#: `<defs>` children that apply GLOBALLY rather than by reference (they legitimately carry no `id`):
#: a `<style>` block or a `<script>`. These are NEVER pruned as "unreferenced templates" — doing so
#: would silently delete document-wide CSS / scripts (a data-loss bug, not an optimization).
_DEFS_KEEP_LOCALS = frozenset({"style", "script"})

#: Attributes carrying geometry/coordinate numbers eligible for precision reduction. The root
#: ``viewBox`` is deliberately EXCLUDED so the user-space coordinate mapping is never altered.
_COORD_ATTRS = frozenset(
    {
        "d",
        "points",
        "transform",
        "gradientTransform",
        "patternTransform",
        "x",
        "y",
        "x1",
        "y1",
        "x2",
        "y2",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "dx",
        "dy",
        "width",
        "height",
        "offset",
        "fx",
        "fy",
    }
)

#: ``url(#id)`` reference inside an attribute/style value.
_URL_REF_RE = re.compile(r"url\(\s*['\"]?#([^'\")\s]+)['\"]?\s*\)")

#: A decimal number (optional sign, optional exponent). Integers (no decimal point) are NOT matched,
#: so arc flags and other integer tokens are never rewritten.
_NUM_RE = re.compile(r"-?\d+\.\d+(?:[eE][-+]?\d+)?")

#: Bounds for the coordinate-precision parameter (decimals).
PRECISION_MIN = 0
PRECISION_MAX = 8
DEFAULT_PRECISION = 2


class OptimizationCounts(BaseModel):
    """Read-only tally of what :func:`optimize_web` would remove/rewrite.

    Mirrors the mutator's cleanup targets one-for-one (⇄ alignment): ``editor_metadata``
    counts editor-only elements + namespaced attributes + comments; ``unused_defs`` the unreferenced
    ``<defs>`` children; ``unreferenced_ids`` the ``id`` attributes nothing references;
    ``empty_groups`` the empty ``<g>``/``<defs>`` containers; ``reducible_coords`` the
    coordinate attributes whose numbers would change at the given precision.
    """

    editor_metadata: int
    unused_defs: int
    unreferenced_ids: int
    empty_groups: int
    reducible_coords: int

    @property
    def total(self) -> int:
        return (
            self.editor_metadata
            + self.unused_defs
            + self.unreferenced_ids
            + self.empty_groups
            + self.reducible_coords
        )


# --- helpers ----------------------------------------------------------------


def _is_comment(node: etree._Element) -> bool:
    return isinstance(node, etree._Comment) or isinstance(node, etree._ProcessingInstruction)


def _attr_namespace(name: str) -> str:
    return (etree.QName(name).namespace or "") if name.startswith("{") else ""


def _attr_local(name: str) -> str:
    return etree.QName(name).localname if name.startswith("{") else name


def _is_editor_attr(node: etree._Element, name: object, aggressive: bool) -> bool:
    """An attribute this pass strips: editor-namespaced ones (layer markers survive unless
    aggressive), and on a kept namedview everything but its id and current layer."""
    if not isinstance(name, str):
        return False
    if local_name(node) == _NAMEDVIEW:
        return name not in ("id", f"{{{INKSCAPE_NS}}}current-layer")
    if _attr_namespace(name) not in _EDITOR_NS:
        return False
    return aggressive or name not in _LAYER_ATTRS


def _is_removed_editor_element(
    node: etree._Element, root: etree._Element, aggressive: bool
) -> bool:
    local = local_name(node)
    if node is root or local not in _EDITOR_ELEMENT_LOCALS:
        return False
    return aggressive or local != _NAMEDVIEW


def _check_precision(precision: int) -> int:
    """Validate `precision` is an int within ``[PRECISION_MIN, PRECISION_MAX]``."""
    if isinstance(precision, bool) or not isinstance(precision, int):
        raise EditError("precision must be an integer")
    if not (PRECISION_MIN <= precision <= PRECISION_MAX):
        raise EditError(f"precision must be between {PRECISION_MIN} and {PRECISION_MAX}")
    return precision


def _round_numbers(value: str, precision: int) -> str:
    """Round each decimal number in ``value`` to ``precision`` decimals, trimming trailing zeros."""

    def repl(match: re.Match[str]) -> str:
        try:
            num = round(float(match.group(0)), precision)
        except (ValueError, OverflowError):  # pragma: no cover - regex already constrains the token
            return match.group(0)
        text = f"{num:.{precision}f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return "0" if text in ("", "-0") else text

    return _NUM_RE.sub(repl, value)


#: `#id` selector in CSS (an id-styled element must keep its id).
_CSS_ID_SELECTOR_RE = re.compile(r"#([A-Za-z_][\w-]*)")


def collect_referenced_ids(root: etree._Element) -> set[str]:
    """Every local id referenced anywhere: ``url(#id)`` in ANY attribute or ``<style>`` text,
    ``#id`` hrefs (``href`` and ``xlink:href``) and SMIL timing values, and ``#id`` CSS selectors.

    Over-collecting is the safe direction: a falsely "referenced" id is merely kept.
    """
    refs: set[str] = set()
    for elem in root.iter():
        if not is_element(elem):
            continue
        if local_name(elem) == "style":
            css = "".join(t for t in elem.itertext() if isinstance(t, str))
            refs.update(m.group(1).strip() for m in _URL_REF_RE.finditer(css))
            refs.update(_CSS_ID_SELECTOR_RE.findall(css))
            continue
        for name, val in elem.attrib.items():
            if not isinstance(val, str) or not val:
                continue
            refs.update(m.group(1).strip() for m in _URL_REF_RE.finditer(val))
            local = _attr_local(name) if isinstance(name, str) else ""
            if local == "href" and val.startswith("#"):
                refs.add(val[1:].strip())
            elif local in ("begin", "end"):  # SMIL: `other.click`, `other.end+1s`
                refs.update(t.split(".")[0].strip() for t in val.split(";") if "." in t)
    refs.discard("")
    return refs


# --- mutation steps ---------------------------------------------------------


def _strip_editor(root: etree._Element, aggressive: bool) -> int:
    """Remove editor-only elements, editor-namespaced attrs, and comments. Returns the count."""
    removed = 0
    for node in list(root.iter()):
        if _is_comment(node):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
                removed += 1
            continue
        if not is_element(node):
            continue
        if _is_removed_editor_element(node, root, aggressive):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
                removed += 1
                continue
        for name in list(node.attrib):
            if _is_editor_attr(node, name, aggressive):
                del node.attrib[name]
                removed += 1
    return removed


def _layer_markers(root: etree._Element) -> bool:
    """True when the tree has a namedview or any layer-marker attribute."""
    return any(
        is_element(e) and (local_name(e) == _NAMEDVIEW or not _LAYER_ATTRS.isdisjoint(e.attrib))
        for e in root.iter()
    )


def _prune_unused_defs(root: etree._Element, referenced: set[str]) -> int:
    """Remove ``<defs>`` children whose id is not referenced (dead templates). Returns the count."""
    removed = 0
    for defs in list(root.iter()):
        if not is_element(defs) or local_name(defs) != "defs":
            continue
        for child in list(defs):
            if not is_element(child) or local_name(child) in _DEFS_KEEP_LOCALS:
                continue
            cid = child.get("id")
            if cid is None or cid not in referenced:
                defs.remove(child)
                removed += 1
    return removed


def _strip_unreferenced_ids(root: etree._Element, referenced: set[str]) -> int:
    """Drop every ``id`` attribute that nothing references. Returns the count."""
    removed = 0
    for elem in root.iter():
        if not is_element(elem):
            continue
        eid = elem.get("id")
        if eid is not None and eid not in referenced:
            del elem.attrib["id"]
            removed += 1
    return removed


def _remove_empty_containers(root: etree._Element) -> int:
    """Remove empty ``<g>``/``<defs>`` (no element children), repeating until stable.

    A container that still carries an ``id`` is kept — by the time this runs, unreferenced ids have
    been stripped, so a remaining id means the element is referenced (e.g. by ``<use href="#g">``).
    """
    removed = 0
    changed = True
    while changed:
        changed = False
        for elem in list(root.iter()):
            if elem is root or not is_element(elem):
                continue
            if local_name(elem) not in _EMPTY_CONTAINER_LOCALS:
                continue
            if elem.get("id") is not None:
                continue
            if any(is_element(child) for child in elem):
                continue
            parent = elem.getparent()
            if parent is not None:
                parent.remove(elem)
                removed += 1
                changed = True
    return removed


def _reduce_precision(root: etree._Element, precision: int) -> int:
    """Round numbers in coordinate attrs to ``precision`` decimals. Returns the rewrite count."""
    changed = 0
    for elem in root.iter():
        if not is_element(elem):
            continue
        for name in list(elem.attrib):
            if not isinstance(name, str) or _attr_local(name) not in _COORD_ATTRS:
                continue
            old = elem.get(name)
            if old is None:
                continue
            new = _round_numbers(old, precision)
            if new != old:
                elem.set(name, new)
                changed += 1
    return changed


# --- public entry points ----------------------------------------------------


def analyze_optimizations(
    root: etree._Element,
    precision: int = DEFAULT_PRECISION,
    keep_ids: list[str] | None = None,
    aggressive: bool = True,
) -> OptimizationCounts:
    """Count (without mutating) what :func:`optimize_web` would remove/rewrite.

    Read-only: used by ``quality_report`` so the optimization opportunities it reports are
    exactly what ``optimize_web`` strips. ``precision`` is clamped into range rather than
    raising (a report should never fail on a stray value). ``keep_ids`` mirrors the optimizer's
    allowlist: listed ids are treated as live, so a kept id is NOT counted as an unreferenced-id /
    unused-defs / empty-group opportunity (keeps the count cross-joinable with an optimize run that
    used the same allowlist). ``quality_report`` calls without it, counting the default.
    ``aggressive`` mirrors the optimizer flag; the default (True) counts every opportunity, which
    is what ``quality_report`` reports.
    """
    precision = max(PRECISION_MIN, min(PRECISION_MAX, precision))
    referenced = collect_referenced_ids(root) | {kid for kid in (keep_ids or []) if kid}

    editor = 0
    for node in root.iter():
        if _is_comment(node):
            editor += 1
            continue
        if not is_element(node):
            continue
        if _is_removed_editor_element(node, root, aggressive):
            editor += 1
            continue
        editor += sum(1 for name in node.attrib if _is_editor_attr(node, name, aggressive))

    unused_defs = 0
    for defs in root.iter():
        if not is_element(defs) or local_name(defs) != "defs":
            continue
        for child in defs:
            if not is_element(child) or local_name(child) in _DEFS_KEEP_LOCALS:
                continue
            if child.get("id") is None or child.get("id") not in referenced:
                unused_defs += 1

    unreferenced_ids = 0
    if aggressive:
        unreferenced_ids = sum(
            1
            for elem in root.iter()
            if is_element(elem) and elem.get("id") is not None and elem.get("id") not in referenced
        )

    empty_groups = 0
    for elem in root.iter():
        if elem is root or not is_element(elem):
            continue
        if local_name(elem) not in _EMPTY_CONTAINER_LOCALS:
            continue
        if elem.get("id") is not None:
            continue
        if not any(is_element(child) for child in elem):
            empty_groups += 1

    reducible = 0
    for elem in root.iter():
        if not is_element(elem):
            continue
        for name in elem.attrib:
            if not isinstance(name, str) or _attr_local(name) not in _COORD_ATTRS:
                continue
            val = elem.get(name)
            if val is not None and _round_numbers(val, precision) != val:
                reducible += 1

    return OptimizationCounts(
        editor_metadata=editor,
        unused_defs=unused_defs,
        unreferenced_ids=unreferenced_ids,
        empty_groups=empty_groups,
        reducible_coords=reducible,
    )


def optimize_web(
    tree: etree._ElementTree,
    precision: int = DEFAULT_PRECISION,
    keep_ids: list[str] | None = None,
    aggressive: bool = False,
) -> str:
    """Web-optimize the working tree (strip cruft, drop dead structure, round).

    ``precision`` (coordinate decimals) is validated eagerly to ``[0, 8]`` so an out-of-range value
    is rejected before any pipeline side effect. Idempotent in spirit:
    re-running on already-optimized output removes nothing further and rounds nothing further.

    ``keep_ids`` is an allowlist of ids that are NEVER stripped as "unreferenced" — a deliberately
    set human/a11y id (e.g. from ``rename_object``) survives the optimize pass even when nothing in
    the document references it (O3). Listed ids are added to the live-reference set,
    so the elements/defs/groups carrying them are preserved too. Unknown ids in the list are
    harmless no-ops.

    By default the document stays EDITABLE: every ``id`` and the layer markers
    (``inkscape:groupmode`` / ``inkscape:label``) survive, so later ops can still target objects by
    id. ``aggressive=True`` also drops unreferenced ids and layer markers (a final ship-only pass).
    """
    precision = _check_precision(precision)
    keep = {kid for kid in (keep_ids or []) if kid}
    root = tree.getroot()
    # Collect referenced ids BEFORE any mutation so stripping editor data can never make a
    # still-referenced id look dead (the safe direction — keep, never break a reference). The
    # caller's keep_ids allowlist is folded in so a deliberately-set id is treated as live.
    referenced = collect_referenced_ids(root) | keep
    markers = _layer_markers(root)
    editor = _strip_editor(root, aggressive)
    defs_removed = _prune_unused_defs(root, referenced)
    ids_removed = _strip_unreferenced_ids(root, referenced) if aggressive else 0
    empties = _remove_empty_containers(root)
    rounded = _reduce_precision(root, precision)
    etree.cleanup_namespaces(root)

    note = "; aggressive: layer markers and namedview removed" if aggressive and markers else ""
    return (
        f"web-optimized: stripped {editor} editor nodes/attrs, removed {defs_removed} unused "
        f"defs, {ids_removed} unreferenced ids, {empties} empty groups, rounded {rounded} "
        f"coordinate attrs (precision {precision}){note}"
    )


__all__ = [
    "DEFAULT_PRECISION",
    "PRECISION_MAX",
    "PRECISION_MIN",
    "OptimizationCounts",
    "analyze_optimizations",
    "collect_referenced_ids",
    "optimize_web",
]
