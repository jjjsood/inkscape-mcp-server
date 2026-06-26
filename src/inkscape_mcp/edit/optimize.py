"""Web-optimization edit engine (E5-04, ADR-005 direct DOM).

Pure ``mutate(tree) -> str`` builder for the one E5 tool that genuinely mutates the document,
``svg_web_optimize``. It edits the parsed working tree IN MEMORY and returns a short human summary,
so it plugs straight into :func:`inkscape_mcp.edit.pipeline.apply_edit` (snapshot + Operation
Record + before/after preview — reversible, medium risk). No MCP decorators here; the
``inkscape_mcp.tools.optimize`` layer wraps this and maps exceptions to ``ToolError``.

Three cleanups, applied in order over the WORKING COPY only (the original is never touched):

1. **Strip editor cruft** — remove Inkscape/sodipodi editor-only elements (``sodipodi:namedview``,
   ``metadata``), every attribute in the Inkscape/sodipodi namespaces, and XML comments, then drop
   the now-unused namespace declarations (``etree.cleanup_namespaces``).
2. **Drop dead structure** — remove unreferenced ``<defs>`` children (dead templates), then every
   ``id`` attribute nothing references, then empty ``<g>``/``<defs>`` containers. The set of
   referenced ids is computed FIRST (``#frag`` / ``url(#frag)`` / ``href="#frag"`` — mirroring the
   E1-08 validate + E2-02 reference-rewrite discipline) so a referenced id is never stripped and no
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
``quality_report`` (E5-05) can surface exactly the opportunities this tool would remove.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from lxml import etree
from pydantic import BaseModel

from inkscape_mcp.document.inspect import INKSCAPE_NS, SODIPODI_NS, XLINK_NS
from inkscape_mcp.edit.dom import EditError

#: The ``mutate`` callback type re-declared locally to avoid importing from the pipeline (which
#: would create an import cycle through the tools layer). Matches
#: :data:`inkscape_mcp.edit.pipeline.MutateFn`.
MutateFn = Callable[[etree._ElementTree], str]

_XLINK_HREF = f"{{{XLINK_NS}}}href"

#: Editor-only namespaces whose attributes are stripped wholesale (editor metadata, not rendering).
_EDITOR_NS = frozenset({INKSCAPE_NS, SODIPODI_NS})

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

#: Attributes that may carry a ``#id`` / ``url(#id)`` reference to another element (mirrors the
#: validate + style engines so optimization preserves exactly the ids those tools consider live).
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

#: ``url(#id)`` reference inside an attribute/style value.
_URL_REF_RE = re.compile(r"url\(\s*['\"]?#([^'\")\s]+)['\"]?\s*\)")

#: A decimal number (optional sign, optional exponent). Integers (no decimal point) are NOT matched,
#: so arc flags and other integer tokens are never rewritten.
_NUM_RE = re.compile(r"-?\d+\.\d+(?:[eE][-+]?\d+)?")

#: Bounds for the coordinate-precision parameter (decimals).
PRECISION_MIN = 0
PRECISION_MAX = 8
DEFAULT_PRECISION = 2


#: Stable per-cleanup codes, IDENTICAL to ``quality_report.opportunities`` keys so the optimizer's
#: ``removed`` map cross-joins with a quality report (E11-08). Order = the order cleanups run.
OPTIMIZE_CODES = (
    "editor_metadata",
    "unused_defs",
    "unreferenced_ids",
    "empty_groups",
    "reducible_coords",
)


class OptimizationCounts(BaseModel):
    """Read-only tally of what :func:`optimize_web_mutate` would remove/rewrite.

    Mirrors the mutator's cleanup targets one-for-one (E5-04 ⇄ E5-05 alignment): ``editor_metadata``
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

    def as_removed_map(self) -> dict[str, int]:
        """``{code: count}`` for every cleanup with a non-zero count, keyed like OPTIMIZE_CODES.

        Codes are IDENTICAL to ``quality_report.opportunities`` keys, so an agent can cross-join the
        optimizer's actual removals against a prior quality report without parsing prose (E11-08).
        """
        return {
            "editor_metadata": self.editor_metadata,
            "unused_defs": self.unused_defs,
            "unreferenced_ids": self.unreferenced_ids,
            "empty_groups": self.empty_groups,
            "reducible_coords": self.reducible_coords,
        }


class WebOptimizeDeltas(BaseModel):
    """Machine-diffable result of one :func:`optimize_web_mutate` run (E11-08).

    ``bytes_before`` / ``bytes_after`` are the serialized working-copy sizes around the mutation
    (so ``bytes_before - bytes_after`` is the saving, no on-disk ``stat`` needed). ``removed`` maps
    each cleanup code (keyed IDENTICALLY to ``quality_report.opportunities``) to the count actually
    removed/rewritten — only non-zero entries are present, so an empty map means a no-op pass.
    """

    bytes_before: int
    bytes_after: int
    removed: dict[str, int]

    @property
    def bytes_saved(self) -> int:
        return self.bytes_before - self.bytes_after


# --- helpers ----------------------------------------------------------------


def _is_element(node: etree._Element) -> bool:
    return isinstance(node.tag, str)


def _is_comment(node: etree._Element) -> bool:
    return isinstance(node, etree._Comment) or isinstance(node, etree._ProcessingInstruction)


def _local(node: etree._Element) -> str:
    return etree.QName(node.tag).localname if _is_element(node) else ""


def _attr_namespace(name: str) -> str:
    return (etree.QName(name).namespace or "") if name.startswith("{") else ""


def _attr_local(name: str) -> str:
    return etree.QName(name).localname if name.startswith("{") else name


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


def collect_referenced_ids(root: etree._Element) -> set[str]:
    """Every local id referenced via ``#id`` (href) or ``url(#id)`` anywhere in the document."""
    refs: set[str] = set()
    for elem in root.iter():
        if not _is_element(elem):
            continue
        href = elem.get(_XLINK_HREF) or elem.get("href")
        if href and href.startswith("#"):
            frag = href[1:].strip()
            if frag:
                refs.add(frag)
        for attr in _REF_ATTRS:
            val = elem.get(attr)
            if not val:
                continue
            for match in _URL_REF_RE.finditer(val):
                frag = match.group(1).strip()
                if frag:
                    refs.add(frag)
    return refs


# --- mutation steps ---------------------------------------------------------


def _strip_editor(root: etree._Element) -> int:
    """Remove editor-only elements, editor-namespaced attrs, and comments. Returns the count."""
    removed = 0
    for node in list(root.iter()):
        if _is_comment(node):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
                removed += 1
            continue
        if not _is_element(node):
            continue
        if node is not root and _local(node) in _EDITOR_ELEMENT_LOCALS:
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
                removed += 1
                continue
        for name in list(node.attrib):
            if isinstance(name, str) and _attr_namespace(name) in _EDITOR_NS:
                del node.attrib[name]
                removed += 1
    return removed


def _prune_unused_defs(root: etree._Element, referenced: set[str]) -> int:
    """Remove ``<defs>`` children whose id is not referenced (dead templates). Returns the count."""
    removed = 0
    for defs in list(root.iter()):
        if not _is_element(defs) or _local(defs) != "defs":
            continue
        for child in list(defs):
            if not _is_element(child) or _local(child) in _DEFS_KEEP_LOCALS:
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
        if not _is_element(elem):
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
            if elem is root or not _is_element(elem):
                continue
            if _local(elem) not in _EMPTY_CONTAINER_LOCALS:
                continue
            if elem.get("id") is not None:
                continue
            if any(_is_element(child) for child in elem):
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
        if not _is_element(elem):
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
) -> OptimizationCounts:
    """Count (without mutating) what :func:`optimize_web_mutate` would remove/rewrite.

    Read-only: used by ``quality_report`` (E5-05) so the optimization opportunities it reports are
    exactly what ``svg_web_optimize`` strips. ``precision`` is clamped into range rather than
    raising (a report should never fail on a stray value). ``keep_ids`` mirrors the optimizer's
    allowlist: listed ids are treated as live, so a kept id is NOT counted as an unreferenced-id /
    unused-defs / empty-group opportunity (keeps the count cross-joinable with an optimize run that
    used the same allowlist — E11-08). ``quality_report`` calls without it, counting the default.
    """
    precision = max(PRECISION_MIN, min(PRECISION_MAX, precision))
    referenced = collect_referenced_ids(root) | {kid for kid in (keep_ids or []) if kid}

    editor = 0
    for node in root.iter():
        if _is_comment(node):
            editor += 1
            continue
        if not _is_element(node):
            continue
        if node is not root and _local(node) in _EDITOR_ELEMENT_LOCALS:
            editor += 1
            continue
        editor += sum(
            1
            for name in node.attrib
            if isinstance(name, str) and _attr_namespace(name) in _EDITOR_NS
        )

    unused_defs = 0
    for defs in root.iter():
        if not _is_element(defs) or _local(defs) != "defs":
            continue
        for child in defs:
            if not _is_element(child) or _local(child) in _DEFS_KEEP_LOCALS:
                continue
            if child.get("id") is None or child.get("id") not in referenced:
                unused_defs += 1

    unreferenced_ids = sum(
        1
        for elem in root.iter()
        if _is_element(elem) and elem.get("id") is not None and elem.get("id") not in referenced
    )

    empty_groups = 0
    for elem in root.iter():
        if elem is root or not _is_element(elem):
            continue
        if _local(elem) not in _EMPTY_CONTAINER_LOCALS:
            continue
        if elem.get("id") is not None:
            continue
        if not any(_is_element(child) for child in elem):
            empty_groups += 1

    reducible = 0
    for elem in root.iter():
        if not _is_element(elem):
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


def optimize_web_mutate(
    precision: int = DEFAULT_PRECISION,
    keep_ids: list[str] | None = None,
    deltas_holder: list[WebOptimizeDeltas] | None = None,
) -> MutateFn:
    """Build a mutate that web-optimizes the working tree (strip cruft, drop dead structure, round).

    ``precision`` (coordinate decimals) is validated eagerly to ``[0, 8]`` so an out-of-range value
    is rejected before any pipeline side effect. The returned closure is idempotent in spirit:
    re-running on already-optimized output removes nothing further and rounds nothing further.

    ``keep_ids`` is an allowlist of ids that are NEVER stripped as "unreferenced" — a deliberately
    set human/a11y id (e.g. from ``rename_object``) survives the optimize pass even when nothing in
    the document references it (E10-07 O3 / E11-04). Listed ids are added to the live-reference set,
    so the elements/defs/groups carrying them are preserved too. Unknown ids in the list are
    harmless no-ops.

    When ``deltas_holder`` is supplied, the closure writes a single :class:`WebOptimizeDeltas`
    (``bytes_before``/``bytes_after`` around the mutation + a ``{code: count}`` ``removed`` map
    keyed like ``quality_report.opportunities``) into it for the tool layer to return (E11-08). The
    human summary is still returned for the Operation Record.
    """
    precision = _check_precision(precision)
    keep = {kid for kid in (keep_ids or []) if kid}

    keep_list = sorted(keep)

    def mutate(tree: etree._ElementTree) -> str:
        root = tree.getroot()
        bytes_before = len(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
        # Reported opportunity counts are taken on the PRE-mutation tree via the SAME detection that
        # `quality_report` uses (`analyze_optimizations`), with the keep_ids allowlist folded in.
        # This makes the returned `removed` map cross-join exactly with a prior `quality_report`
        # (E11-08) — independent-per-category, not the order-dependent removal tallies (where an
        # earlier step would otherwise consume what a later step counts).
        reported = (
            analyze_optimizations(root, precision, keep_ids=keep_list)
            if deltas_holder is not None
            else None
        )

        # Collect referenced ids BEFORE any mutation so stripping editor data can never make a
        # still-referenced id look dead (the safe direction — keep, never break a reference). The
        # caller's keep_ids allowlist is folded in so a deliberately-set id is treated as live.
        referenced = collect_referenced_ids(root) | keep
        editor = _strip_editor(root)
        defs_removed = _prune_unused_defs(root, referenced)
        ids_removed = _strip_unreferenced_ids(root, referenced)
        empties = _remove_empty_containers(root)
        rounded = _reduce_precision(root, precision)
        etree.cleanup_namespaces(root)

        if deltas_holder is not None and reported is not None:
            bytes_after = len(etree.tostring(tree, xml_declaration=True, encoding="UTF-8"))
            deltas_holder[:] = [
                WebOptimizeDeltas(
                    bytes_before=bytes_before,
                    bytes_after=bytes_after,
                    removed={code: n for code, n in reported.as_removed_map().items() if n > 0},
                )
            ]

        return (
            f"web-optimized: stripped {editor} editor nodes/attrs, removed {defs_removed} unused "
            f"defs, {ids_removed} unreferenced ids, {empties} empty groups, rounded {rounded} "
            f"coordinate attrs (precision {precision})"
        )

    return mutate


__all__ = [
    "DEFAULT_PRECISION",
    "OPTIMIZE_CODES",
    "PRECISION_MAX",
    "PRECISION_MIN",
    "MutateFn",
    "OptimizationCounts",
    "WebOptimizeDeltas",
    "analyze_optimizations",
    "collect_referenced_ids",
    "optimize_web_mutate",
]
