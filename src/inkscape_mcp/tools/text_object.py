"""Text / object edit tools (E2-02): ``replace_text`` / ``set_font`` / ``duplicate_object`` /
``rename_object``.

Thin MCP layer over the direct-DOM engines in :mod:`inkscape_mcp.edit.text_object`. Each tool
builds the engine's ``mutate`` closure and hands it to the shared, reversible edit pipeline
(`apply_edit`), so every change is snapshotted, recorded as an Operation Record, and linked to a
before/after preview (ADR-004). Editing is direct lxml on the DOM only (ADR-005); these tools are
small and typed (no portmanteau) per ADR-002.

This layer maps engine / pipeline exceptions to `ToolError` with STABLE, host-path-free messages
(sec.12): an unknown document id becomes ``"document id not found"``, a missing object id becomes
``"object id not found in document"``, an unparseable working copy becomes ``"document could not
be parsed safely"``, and a validation failure surfaces its already-safe ``EditError`` message.
All four are medium risk (write-new / text / transform on the working copy, reversible).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp.exceptions import ToolError
from lxml import etree
from pydantic import BaseModel, Field

from inkscape_mcp.document.inspect import (
    DocumentNotFound,
    InspectionError,
    _build_parent_map,
    _document_css_rules,
    _font_families_from_value,
    _load_tree,
    _own_paint_value,
)
from inkscape_mcp.edit.dom import EditError, TargetNotFound
from inkscape_mcp.edit.pipeline import EditApplyError, EditResult, MutateFn, apply_edit
from inkscape_mcp.edit.text_object import (
    make_duplicate_object,
    make_rename_object,
    make_replace_text,
    make_set_font,
)
from inkscape_mcp.fonts.coverage import suggest_covering_family, uncovered_chars
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.server import mcp

_logger = get_logger("tools.text_object")


class FontCoverage(BaseModel):
    """Per-target glyph-coverage outcome for a `set_font` family change (E16-04).

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


class SetFontResult(EditResult):
    """`EditResult` for `set_font`, additively extended with glyph-coverage at apply time (E16-04).

    All `EditResult` fields are preserved (back-compat). `coverage_ok` is False when ANY edited text
    object now names a family that cannot render its characters; `font_coverage` lists the
    per-object detail for objects whose coverage could be determined. When the family was not
    changed, the document has no fontconfig, or the family is not installed, coverage is left
    unknown and `coverage_ok` stays True with an empty `font_coverage` (the `missing_font` validate
    check owns the not-installed case).
    """

    coverage_ok: bool = True
    font_coverage: list[FontCoverage] = Field(default_factory=list)


def _coverage_for_targets(doc_id: str, object_ids: list[str]) -> tuple[bool, list[FontCoverage]]:
    """Compute per-target glyph coverage from the POST-edit working copy (E16-04).

    Re-reads the working tree, resolves each target's effective first `font-family` (cascade +
    inheritance, the SAME resolution validate uses), and tests its text against the family's OWN
    cmap. Returns `(coverage_ok, details)`: `coverage_ok` is False iff some target has a non-empty
    uncovered set; `details` carries one entry per target whose coverage was determinable. Best
    effort — never raises (a parse/font fault yields `(True, [])` so a coverage probe can't break
    the edit that already succeeded).
    """
    try:
        _entry, root = _load_tree(doc_id)
    except (DocumentNotFound, InspectionError):
        return True, []

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
        family = _resolve_first_family(target, css_rules, parents)
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


def _resolve_first_family(
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


def _apply(
    doc_id: str, tool: str, params: dict[str, Any], build_mutate: Callable[[], MutateFn]
) -> EditResult:
    """Run one edit through the pipeline, mapping engine errors to stable `ToolError`s.

    `build_mutate` constructs the engine's `mutate` closure. It is called INSIDE the try block so
    that input validation performed at build time (e.g. an injection-y font value, an empty id
    list, a malformed `new_id`) raises `EditError` here and is mapped to `ToolError` exactly like a
    validation failure raised during the mutation itself. Centralizes the exception-to-message
    mapping shared by all four tools so each surfaces the same host-path-free failures (sec.12).
    """
    try:
        return apply_edit(doc_id, tool, params, build_mutate())
    except (EditApplyError, DocumentNotFound, KeyError) as exc:
        raise ToolError("document id not found") from exc
    except TargetNotFound as exc:
        raise ToolError("object id not found in document") from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc
    except EditError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
def replace_text(doc_id: str, object_id: str, text: str) -> EditResult:
    """Replace the text content of a text element (`<text>` / `<tspan>` / flow text).

    When to use: changing what a text object says (get its id from `find_objects`). To change the
    font/size use `set_font`; to rename the element's id use `rename_object`.

    Key params: `object_id` must be a text-bearing element; `text` is length-bounded and may not
    contain control characters other than tab / newline / carriage return. If the `<text>` has
    `<tspan>` children they are dropped and the content collapses to a single run.

    Return shape: `EditResult` — `operation_id`, `snapshot_id`, `changed`, before/after preview; the
    edit lands on the working copy only (reversible).

    Example: `replace_text(doc_id, "title", "Hello")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible text edit on the working copy; original untouched).
    """
    result = _apply(
        doc_id,
        "replace_text",
        {"object_id": object_id, "text_length": len(text)},
        lambda: make_replace_text(object_id, text),
    )
    log_tool_call(
        _logger,
        tool="replace_text",
        doc_id=doc_id,
        object_id=object_id,
        operation_id=result.operation_id,
    )
    return result


@mcp.tool
def set_font(
    doc_id: str,
    object_ids: list[str],
    family: str | None = None,
    size: str | None = None,
    weight: str | None = None,
) -> SetFontResult:
    """Set font-family / font-size / font-weight on one or more text objects.

    When to use: restyling text typography. To change the words use `replace_text`; for non-font
    fill/stroke use `set_fill` / `set_stroke`.

    Key params: provide any of `family`, `size`, `weight` (at least one required); each is validated
    and written to every target's inline style.

    Return shape: `SetFontResult` — all `EditResult` fields (`operation_id`, `snapshot_id`,
    `changed`, before/after preview; the edit lands on the working copy only, reversible) PLUS glyph
    coverage (E16-04): `coverage_ok` is False when a target now names a family that cannot render
    its text, and `font_coverage` lists per object the `uncovered_chars` (read from the font's OWN
    cmap, never fontconfig substitution) and a `suggested_family` that covers them — so a
    non-covering font choice is checkable at apply time instead of silently shipping tofu.

    Example: `set_font(doc_id, ["title"], family="Inter", size="24px")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible style edit on the working copy; original untouched).
    """
    result = _apply(
        doc_id,
        "set_font",
        {
            "object_ids": object_ids,
            "family": family,
            "size": size,
            "weight": weight,
        },
        lambda: make_set_font(object_ids, family=family, size=size, weight=weight),
    )
    coverage_ok, font_coverage = _coverage_for_targets(doc_id, object_ids)
    log_tool_call(
        _logger,
        tool="set_font",
        doc_id=doc_id,
        count=len(object_ids),
        operation_id=result.operation_id,
        coverage_ok=coverage_ok,
    )
    return SetFontResult(
        **result.model_dump(),
        coverage_ok=coverage_ok,
        font_coverage=font_coverage,
    )


@mcp.tool
def duplicate_object(doc_id: str, object_id: str, new_id: str | None = None) -> EditResult:
    """Duplicate an object or group in place, inserting the clone right after the original.

    When to use: copying one object once. To copy into a grid use `tile`; to instance via `<use>`
    use `create_use`; to change an id without copying use `rename_object`.

    Key params: the clone re-ids every contained id uniquely and rewrites its internal references so
    it is self-consistent. An optional `new_id` (validated safe and unused) names the clone's top
    element; otherwise a suffixed id is generated.

    Return shape: `EditResult` — `operation_id`, `snapshot_id`, `changed`, before/after preview; the
    new top id is reported in the `summary`. Lands on the working copy only (reversible).

    Example: `duplicate_object(doc_id, "icon", new_id="icon_copy")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible write-new on the working copy; original untouched).
    """
    result = _apply(
        doc_id,
        "duplicate_object",
        {"object_id": object_id, "new_id": new_id},
        lambda: make_duplicate_object(object_id, new_id=new_id),
    )
    log_tool_call(
        _logger,
        tool="duplicate_object",
        doc_id=doc_id,
        object_id=object_id,
        operation_id=result.operation_id,
    )
    return result


@mcp.tool
def rename_object(
    doc_id: str,
    object_id: str,
    new_id: str | None = None,
    label: str | None = None,
) -> EditResult:
    """Change an object's `id` and/or its `inkscape:label`.

    When to use: giving an object a stable/human id or label. To copy it use `duplicate_object`; to
    keep an id surviving `svg_web_optimize` add it to that tool's `keep_ids`.

    Key params: provide `new_id` and/or `label` (at least one required). Changing the id validates
    the new id (safe charset, not already used) and rewrites all in-document references to the old
    id so nothing dangles. `label` is set on `inkscape:label`.

    Return shape: `EditResult` — `operation_id`, `snapshot_id`, `changed`, before/after preview; the
    edit lands on the working copy only (reversible).

    Example: `rename_object(doc_id, "rect12", new_id="header", label="Header bar")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: medium (reversible edit on the working copy; original untouched).
    """
    result = _apply(
        doc_id,
        "rename_object",
        {"object_id": object_id, "new_id": new_id, "label": label},
        lambda: make_rename_object(object_id, new_id=new_id, label=label),
    )
    log_tool_call(
        _logger,
        tool="rename_object",
        doc_id=doc_id,
        object_id=object_id,
        operation_id=result.operation_id,
    )
    return result
