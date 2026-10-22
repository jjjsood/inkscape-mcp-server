"""Direct-DOM text / object edit engines (ADR-005).

Pure ``fn(tree, ...) -> str`` functions for the four tools: ``replace_text``,
``set_font``, ``duplicate_object``, and ``rename_object``. Each edits the parsed working tree
IN MEMORY and returns a short human summary of what changed; none carries an ``@mcp.tool``
decorator. They are wired into the shared, reversible edit pipeline (`apply_edit`) by the thin
tool layer, so every change gets a snapshot + Operation Record + before/after preview (ADR-004).

All editing is direct lxml on the DOM (no Inkscape engine) per ADR-005, reusing the validated
primitives in :mod:`inkscape_mcp.edit.dom`. Client inputs that reach these engines are object
ids (used only for in-tree lookups, never argv) and typed edit parameters; every value that
lands in an attribute or inline ``style`` block is validated by a ``dom`` normalizer or by a
pattern check here, so a caller can never inject extra markup, CSS declarations, or control
characters (sec.12).
"""

from __future__ import annotations

import re
from typing import Any

from lxml import etree
from pydantic import BaseModel

from inkscape_mcp.document.inspect import (
    _build_parent_map,
    _document_css_rules,
    _font_families_from_value,
    _own_paint_value,
    local_name,
)
from inkscape_mcp.edit.dom import (
    SAFE_ID_RE,
    EditError,
    all_ids,
    deep_copy_with_new_ids,
    find_by_id,
    normalize_font_family,
    normalize_font_weight,
    normalize_length,
    require_target,
    rewrite_references,
    set_label,
    set_style_property,
)
from inkscape_mcp.fonts.coverage import suggest_covering_family, uncovered_chars

#: Maximum text-content length accepted by :func:`replace_text` (characters). Bounds how much a
#: single edit can write into a text node; well above any realistic label/paragraph.
_MAX_TEXT_LEN = 100_000

#: Maximum length of an ``inkscape:label`` written by :func:`rename_object`. Bounds an otherwise
#: unbounded attribute write driven by client input.
_MAX_LABEL_LEN = 256

#: Control characters that must never enter text content. Everything in the C0/C1 ranges except
#: tab (\t), newline (\n), and carriage return (\r), which are legitimate whitespace.
_FORBIDDEN_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

#: Local names of SVG elements whose primary purpose is to carry text content.
_TEXT_TAGS = frozenset({"text", "tspan", "textPath", "tref", "flowRoot", "flowPara", "flowSpan"})


class FontCoverage(BaseModel):
    """Per-target glyph-coverage outcome for a `set_font` family change.

    `object_id` is the edited text object; `family` is the effective FIRST font-family now applied
    to it; `uncovered_chars` is the string of characters that family's OWN cmap cannot render (empty
    when fully covered); `suggested_family` is a cmap-verified family that covers the uncovered
    characters (None when coverage is fine or none was found). Coverage is read from the font file's
    cmap, never from fontconfig auto-substitution.
    """

    object_id: str
    family: str
    uncovered_chars: str
    suggested_family: str | None = None


def resolve_first_family(
    elem: etree._Element,
    css_rules: list[Any],
    parents: dict[etree._Element, etree._Element],
) -> str | None:
    """Effective first `font-family` for a text element (cascade + inheritance), or None.

    Mirrors the validate engine's resolution: `<style>` rules < presentation attr < inline style,
    inheriting from the nearest ancestor that sets it; returns the FIRST family in the comma list.
    """
    current: etree._Element | None = elem
    while current is not None:
        own = _own_paint_value(current, css_rules, "font-family")
        if own is not None and own.strip().lower() != "inherit":
            families = _font_families_from_value(own)
            return families[0] if families else None
        current = parents.get(current)
    return None


def font_coverage_for_root(
    root: etree._Element, object_ids: list[str]
) -> tuple[bool, list[FontCoverage]]:
    """Compute per-target glyph coverage on a parsed (possibly in-memory) tree.

    Resolves each target's effective first `font-family` (cascade + inheritance, the SAME resolution
    validate uses) and tests its text against the family's OWN cmap. Returns `(coverage_ok,
    details)`: `coverage_ok` is False iff some target has a non-empty uncovered set; `details`
    carries one entry per target whose coverage was determinable. Best effort — a target that is
    missing, has no family, no text, or whose font is unknown is skipped.
    """
    css_rules = _document_css_rules(root)
    parents = _build_parent_map(root)
    by_id: dict[str, etree._Element] = {}
    for elem in root.iter():
        if isinstance(elem.tag, str):
            eid = elem.get("id")
            if eid is not None:
                by_id.setdefault(eid, elem)

    details: list[FontCoverage] = []
    ok = True
    for oid in object_ids:
        target = by_id.get(oid)
        if target is None:
            continue
        family = resolve_first_family(target, css_rules, parents)
        if not family:
            continue
        text = "".join(t for t in target.itertext() if isinstance(t, str))
        if not text.strip():
            continue
        missing = uncovered_chars(family, text)
        if missing is None:
            continue  # coverage unknown (not installed / no fontconfig) — owned by missing_font
        suggestion = suggest_covering_family(missing) if missing else None
        if missing:
            ok = False
        details.append(
            FontCoverage(
                object_id=oid,
                family=family,
                uncovered_chars=missing,
                suggested_family=suggestion,
            )
        )
    return ok, details


def replace_text(tree: etree._ElementTree, object_id: str, text: str) -> str:
    """Replace a text element's content.

    The target (resolved by ``require_target``) must be a text-bearing element
    (``<text>``, ``<tspan>``, ``<textPath>``, flow text, ...); otherwise :class:`EditError` is
    raised. ``text`` is bounded to ``_MAX_TEXT_LEN`` characters and may not contain control
    characters other than tab / newline / carriage return.

    tspan handling: a ``<text>`` whose only content is ONE plain ``<tspan>`` (Inkscape's one-line
    text) keeps that tspan and only its content changes. Otherwise, when the target contains
    ``<tspan>`` children, the content is COLLAPSED to a single text run: all children are
    dropped and the new string is assigned as the element's ``.text`` (the caller asked to
    replace the text, not to preserve per-span styling); callers who need per-span control
    should target the specific ``<tspan>`` by its id instead. Assigning via lxml ``.text``
    means the value is stored as a text node, so no markup injection is possible.
    """
    if len(text) > _MAX_TEXT_LEN:
        raise EditError(f"text too long: {len(text)} > {_MAX_TEXT_LEN} characters")
    if _FORBIDDEN_CTRL_RE.search(text):
        raise EditError("text contains forbidden control characters")
    root = tree.getroot()
    elem = require_target(root, object_id)
    if local_name(elem) not in _TEXT_TAGS:
        raise EditError(f"object {object_id!r} is not a text element")

    # Inkscape writes a one-line text as <text><tspan sodipodi:role="line">..</tspan></text>;
    # keep that single run (its position, style and id) and replace only its content.
    children = [c for c in elem if isinstance(c.tag, str)]
    if (
        len(children) == 1
        and local_name(children[0]) == "tspan"
        and len(children[0]) == 0
        and not (elem.text or "").strip()
    ):
        children[0].text = text
        return f"replaced text content of {object_id!r}"

    # Collapse any child runs to a single text node, then assign the new content. Dropping
    # children removes nested tspans/markup so the result is exactly the requested string.
    for child in list(elem):
        elem.remove(child)
    elem.text = text
    return f"replaced text content of {object_id!r}"


def set_font(
    tree: etree._ElementTree,
    object_ids: list[str],
    family: str | None = None,
    size: str | None = None,
    weight: str | None = None,
) -> str:
    """Set font properties on one or more targets.

    Sets any provided of ``font-family`` (via ``normalize_font_family``), ``font-size`` (via
    ``normalize_length``), and ``font-weight`` (via ``normalize_font_weight``) on each target's
    inline style using ``set_style_property``. At least one of the three must be supplied and
    ``object_ids`` must be non-empty; otherwise :class:`EditError` is raised. Only the requested
    family is written — missing-font detection stays with ``validate_document``.
    """
    if not object_ids:
        raise EditError("no target object ids supplied")
    if family is None and size is None and weight is None:
        raise EditError("set_font requires at least one of family / size / weight")

    # Validate (and canonicalize) every value up front so a bad input fails before any mutation.
    props: list[tuple[str, str]] = []
    if family is not None:
        props.append(("font-family", normalize_font_family(family)))
    if size is not None:
        props.append(("font-size", normalize_length(size)))
    if weight is not None:
        props.append(("font-weight", normalize_font_weight(weight)))
    root = tree.getroot()
    targets = [require_target(root, oid) for oid in object_ids]
    for elem in targets:
        for prop, value in props:
            set_style_property(elem, prop, value)
    set_props = ", ".join(f"{p}={v}" for p, v in props)
    return f"set {set_props} on {len(targets)} object(s)"


def duplicate_object(tree: etree._ElementTree, object_id: str, new_id: str | None = None) -> str:
    """Duplicate an object/group in place.

    The target is deep-copied via ``deep_copy_with_new_ids`` (which re-ids every id in the clone
    uniquely and rewrites intra-clone references), and the clone is inserted IMMEDIATELY AFTER
    the original in the same parent so it stacks just above it. A supplied ``new_id`` is validated
    (safe charset, unused) before the copy; ``deep_copy_with_new_ids`` additionally raises on a
    collision. The clone's top id is reported in the summary.
    """
    if new_id is not None and not SAFE_ID_RE.match(new_id):
        raise EditError(f"invalid object id: {new_id!r}")
    root = tree.getroot()
    elem = require_target(root, object_id)
    parent = elem.getparent()
    if parent is None:
        raise EditError(f"object {object_id!r} cannot be duplicated (it is the document root)")
    if new_id is not None and new_id in all_ids(root):
        raise EditError(f"id already in use: {new_id!r}")

    clone, top_id = deep_copy_with_new_ids(elem, root, new_id)
    parent.insert(parent.index(elem) + 1, clone)
    return f"duplicated {object_id!r} as {top_id!r}"


def rename_object(
    tree: etree._ElementTree, object_id: str, new_id: str | None = None, label: str | None = None
) -> str:
    """Change an object's ``id`` and/or ``inkscape:label``.

    At least one of ``new_id`` / ``label`` must be supplied (else :class:`EditError`). When
    changing the id, ``new_id`` is validated (safe charset, not already used) and ALL in-document
    references to the old id (``#id`` / ``url(#id)`` / ``href="#id"``) are rewritten via
    ``rewrite_references`` so nothing is left dangling. The label is set with ``set_label`` and is
    bounded to ``_MAX_LABEL_LEN`` characters with control characters rejected (same guard as
    :func:`replace_text`) so a caller cannot drive an unbounded or control-laden attribute write.
    """
    if new_id is None and label is None:
        raise EditError("rename_object requires at least one of new_id / label")
    if new_id is not None and not SAFE_ID_RE.match(new_id):
        raise EditError(f"invalid object id: {new_id!r}")
    if label is not None:
        if len(label) > _MAX_LABEL_LEN:
            raise EditError(f"label too long: {len(label)} > {_MAX_LABEL_LEN} characters")
        if _FORBIDDEN_CTRL_RE.search(label):
            raise EditError("label contains forbidden control characters")
    root = tree.getroot()
    elem = require_target(root, object_id)
    changes: list[str] = []

    if new_id is not None:
        if new_id == object_id:
            raise EditError("new_id is the same as the current id")
        if new_id in all_ids(root):
            raise EditError(f"id already in use: {new_id!r}")
        elem.set("id", new_id)
        rewrite_references(root, {object_id: new_id})
        changes.append(f"id {object_id!r} -> {new_id!r}")

    if label is not None:
        set_label(elem, label)
        changes.append(f"label -> {label!r}")

    return "renamed object: " + "; ".join(changes)


def delete_objects(tree: etree._ElementTree, object_ids: list[str]) -> str:
    """Remove objects by id from the DOM.

    Each id in ``object_ids`` (≥ 1 required, else :class:`EditError`) is looked up and detached from
    its parent. The document root itself cannot be deleted (it has no parent); attempting to delete
    it raises :class:`EditError`.

    NO-MATCH / no-op hygiene: ids that are NOT present in the document are simply
    SKIPPED — a missing id is not an error here (unlike the single-target edits) because deleting an
    already-absent object is a successful no-op. When NONE of the supplied ids exist the tree is
    left untouched, the canonical bytes are byte-identical, and the shared edit pipeline
    (`apply_edit`)
    reports ``changed=False`` and writes NO snapshot / Operation Record. The list of ids that were
    actually removed is reported in the summary so the tool can surface the affected ids.

    Object ids are used solely for in-tree lookup (never argv); deletion is pure lxml on the parsed
    working tree, so no markup/injection is possible (sec.12).
    """
    if not object_ids:
        raise EditError("delete_object requires at least one object id")
    root = tree.getroot()
    removed: list[str] = []
    for oid in object_ids:
        elem = find_by_id(root, oid)
        if elem is None:
            continue  # already absent — a successful no-op for this id
        parent = elem.getparent()
        if parent is None:
            raise EditError(f"cannot delete the document root ({oid!r})")
        parent.remove(elem)
        removed.append(oid)
    if not removed:
        # None of the ids existed: leave the tree untouched so the pipeline sees a true no-op.
        return "no matching objects to delete"
    return f"deleted {len(removed)} object(s): {', '.join(repr(i) for i in removed)}"


__all__ = [
    "FontCoverage",
    "delete_objects",
    "duplicate_object",
    "font_coverage_for_root",
    "rename_object",
    "replace_text",
    "set_font",
]
