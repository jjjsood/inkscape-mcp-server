"""Locked-layer change report, shared by every mutating edit (pipeline-level).

Inkscape marks a locked layer/group with ``sodipodi:insensitive="true"``. Editing inside one is
allowed (a document-wide recolour may well be intended), but must never be silent: the caller
fingerprints the locked objects before and after the mutation and reports what moved.

The fingerprint is per element (tag, attributes, own text, and the ``<style>`` rules that match
it), so a change that reaches an object only through a CSS rule (``replace_color`` rewriting
``.accent``) is detected too. Elements without an ``id`` are not reported.
"""

from __future__ import annotations

from typing import Any

from lxml import etree

from inkscape_mcp.document.inspect import (
    _document_css_rules,
    _element_classes,
    is_element,
    local_name,
)

_INSENSITIVE = "{http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd}insensitive"
_MAX_LISTED = 8

#: id -> (outermost locked ancestor-or-self id, fingerprint)
LockedState = dict[str, tuple[str, tuple[Any, ...]]]


def locked_state(root: etree._Element) -> LockedState:
    """Fingerprint every id'd element that is, or sits inside, a locked layer/group."""
    if not any(is_element(e) and e.get(_INSENSITIVE) == "true" for e in root.iter()):
        return {}
    rules = _document_css_rules(root)
    out: LockedState = {}

    def walk(elem: etree._Element, lock: str | None) -> None:
        if lock is None and elem.get(_INSENSITIVE) == "true":
            lock = elem.get("id") or "?"
        oid = elem.get("id")
        if lock is not None and oid:
            tag, classes = local_name(elem), _element_classes(elem)
            sig = tuple(
                (r.tag, r.cls, r.elem_id, tuple(sorted(r.decls.items())))
                for r in rules
                if r.matches(tag=tag, classes=classes, elem_id=oid)
            )
            out[oid] = (
                lock,
                (tag, tuple(sorted(elem.attrib.items())), (elem.text or "").strip(), sig),
            )
        for child in elem:
            if is_element(child):
                walk(child, lock)

    walk(root, None)
    return out


def locked_changes(before: LockedState, after: LockedState) -> dict[str, list[str]]:
    """Locked layer id -> sorted ids of objects the edit changed, added or removed there.

    A locked layer the edit itself created (e.g. the copies `grid` makes) is not reported.
    """
    changed: dict[str, list[str]] = {}
    old_locks = {lock for lock, _ in before.values()}
    for oid in before.keys() | after.keys():
        b, a = before.get(oid), after.get(oid)
        if b is None and a is not None and a[0] not in old_locks:
            continue
        if b is None or a is None or b[1] != a[1]:
            changed.setdefault((a or b)[0], []).append(oid)  # type: ignore[index]
    return {lock: sorted(ids) for lock, ids in sorted(changed.items())}


def locked_notes(before: LockedState, after: LockedState) -> list[str]:
    """One note per locked layer that had objects changed, added or removed by the edit."""
    notes = []
    for lock, ids in locked_changes(before, after).items():
        shown = ", ".join(ids[:_MAX_LISTED]) + (", ..." if len(ids) > _MAX_LISTED else "")
        noun = "object" if len(ids) == 1 else "objects"
        notes.append(
            f"changed {len(ids)} {noun} in locked layer {lock!r}: {shown} "
            "(locks do not block server edits; history(action='undo') if unintended)"
        )
    return notes
