"""Document tools: `open_document` + `inspect_document`.

Thin MCP layer over the registry (open) and the reusable inspection engine
(`inkscape_mcp.document.inspect`). Direct DOM only (ADR-005), read-only inspection.
All client-facing errors are raised as `ToolError` with stable, host-path-free messages
(fastmcp-patterns error model / sec.12).
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from inkscape_mcp.document.inspect import (
    DocAssets,
    DocFonts,
    DocLayers,
    DocStyles,
    DocSummary,
    DocTree,
    DocumentNotFound,
    InspectionError,
    ObjectRef,
    inspect_assets,
    inspect_fonts,
    inspect_layers,
    inspect_styles,
    inspect_summary,
    inspect_tree,
    list_objects,
)
from inkscape_mcp.edit.compose import build_blank_svg
from inkscape_mcp.edit.dom import EditError
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.registry import get_registry
from inkscape_mcp.server import mcp
from inkscape_mcp.snapshots import create_snapshot
from inkscape_mcp.workspace.limits import LimitExceeded
from inkscape_mcp.workspace.paths import SandboxViolation
from inkscape_mcp.workspace.xml_safety import UnsafeXMLError

_logger = get_logger("tools.document")


class OpenDocumentResult(BaseModel):
    """Result of `open_document`: the new opaque id plus its summary."""

    doc_id: str
    summary: DocSummary


class InspectDocumentResult(BaseModel):
    """Aggregate inspection: summary, tree, layers, styles, fonts, external assets, and the
    addressable object list.

    `objects` is the flat list of every id-bearing object (the same `ObjectRef` shape `find_objects`
    returns) so an agent can discover targetable ids — and their tag / bbox / paint / text — for the
    id-taking edit tools without a second call. Added additively."""

    summary: DocSummary
    tree: DocTree
    layers: DocLayers
    styles: DocStyles
    fonts: DocFonts
    assets: DocAssets
    objects: list[ObjectRef]


class ReloadDocumentResult(BaseModel):
    """Result of `reload_document`: the refreshed summary plus the pre-reload snapshot link.

    `pre_reload_snapshot_id` is the snapshot of the working copy taken BEFORE the reload, so the
    reload is itself reversible (restore it to undo the refresh). `summary` reflects the document
    AFTER the reload (its source bytes).
    """

    doc_id: str
    pre_reload_snapshot_id: str
    summary: DocSummary


@mcp.tool
def open_document(path: str) -> OpenDocumentResult:
    """Open an SVG into a tracked workspace document and return its id + summary.

    When to use: the entry point for working on an EXISTING file — you need the `doc_id` before any
    other tool. To start from nothing use `create_document`; to adopt agent-composed SVG use
    `set_document_svg` / `insert_svg_fragment`; to resync external edits use `reload_document`.

    Key params: `path` may be workspace-RELATIVE (anchored to the first workspace root, NOT the
    server CWD — matching `save_document_as` / `live_sync_to_workspace`) or absolute; either is
    sandbox-validated and a `../`-escape, an absolute path outside the workspace, or a symlink whose
    target leaves the sandbox is rejected with `path rejected: outside workspace`.
    WORKING-COPY MODEL: opening copies your source SVG byte-for-byte into a per-document workspace
    as an immutable `original.svg` and seeds a single live WORKING COPY. The returned `doc_id`
    addresses that copy; EVERY subsequent tool operates on it, and your ORIGINAL is NEVER mutated.
    Edits are reversible (pre-edit snapshot + Operation Record); `restore_snapshot` rolls back.

    Return shape: `OpenDocumentResult` — `doc_id` (opaque, pass to every other tool) and `summary`
    (size, viewBox, units, counts).

    Example: `open_document("logo.svg")`

    Risk class: low (opens via working copy; original never mutated).
    """
    try:
        entry = get_registry().open_document(path)
    except SandboxViolation as exc:
        # exc.args[0] is already a SAFE public message (no host path), prefixed
        # "path rejected: ..." by the sandbox layer.
        _logger.error("open_document rejected", extra={"detail": exc.detail})
        raise ToolError(str(exc)) from exc
    except LimitExceeded as exc:
        _logger.error("open_document over limit", extra={"detail": str(exc)})
        raise ToolError("input file exceeds the configured size limit") from exc
    except UnsafeXMLError as exc:
        _logger.error("open_document unsafe xml", extra={"detail": str(exc)})
        raise ToolError("document could not be parsed safely") from exc

    log_tool_call(_logger, tool="open_document", doc_id=entry.doc_id)
    summary = inspect_summary(entry.doc_id)
    return OpenDocumentResult(doc_id=entry.doc_id, summary=summary)


@mcp.tool
def create_document(
    width: float,
    height: float,
    viewBox: str | None = None,
    background: str | None = None,
) -> OpenDocumentResult:
    """Create a blank, tracked working-copy document from scratch — NO source file required.

    When to use: starting fresh authoring with the `create_*` / compose tools when there is no SVG
    to open. To open an EXISTING file use `open_document`; to set the whole SVG body afterwards use
    `set_document_svg`.

    Key params: `width` / `height` are the page size in user units (both > 0). `viewBox` is an
    optional explicit `"minx miny w h"` box (a `0 0 width height` box is synthesized when omitted,
    so the document is never viewBox-less). `background` is an optional validated colour (hex /
    `rgb()` / `hsl()` / named keyword — never CSS-injectable) painted as a full-page rect; omit for
    a transparent page. The generated document is `validate_document`-clean.

    Return shape: `OpenDocumentResult` (same as `open_document`) — `doc_id` (addresses a fully
    tracked working copy: snapshots, reversibility, reload) and `summary`.

    Example: `create_document(800, 600, background="#ffffff")`

    Risk class: medium (creates a new tracked document; no existing state mutated).
    """
    try:
        # `build_blank_svg` reuses the `dom` validators (size / viewBox / colour), which raise
        # `EditError` (the parent of `ComposeError`); catch the parent so an injection-y colour or a
        # bad size/viewBox surfaces its already-safe message as a `ToolError`.
        svg_bytes = build_blank_svg(width, height, viewbox=viewBox, background=background)
    except EditError as exc:
        raise ToolError(str(exc)) from exc

    try:
        entry = get_registry().create_document(svg_bytes)
    except LimitExceeded as exc:
        raise ToolError("input exceeds the configured size limit") from exc

    log_tool_call(_logger, tool="create_document", doc_id=entry.doc_id)
    summary = inspect_summary(entry.doc_id)
    return OpenDocumentResult(doc_id=entry.doc_id, summary=summary)


@mcp.tool
def reload_document(doc_id: str) -> ReloadDocumentResult:
    """Refresh a working copy FROM ITS SOURCE under the SAME `doc_id`, discarding working edits.

    When to use: external edits changed the source file and you want to resync in place (keep the
    same `doc_id`). To undo a single edit instead use `restore_snapshot`; to open a different file
    use `open_document`.

    Key params: `doc_id` must be open. Flow (reversible): take a PRE-reload snapshot of the current
    working copy (undo via `restore_snapshot`), re-resolve the source through the sandbox and
    re-validate it is STILL inside the workspace (a moved/vanished source is rejected with a stable
    "path rejected" message), then re-copy the source over the working copy. A `create_document`
    document has no external source, so its reload restores from its blank seed.

    Return shape: `ReloadDocumentResult` — refreshed `summary` plus `pre_reload_snapshot_id` (the
    pre-reload checkpoint).

    Example: `reload_document(doc_id)`

    Risk class: low (only the working copy is rewritten, reversibly; the original is never written).
    """
    reg = get_registry()
    try:
        reg.get(doc_id)
    except KeyError as exc:
        raise ToolError("document id not found") from exc

    # Pre-reload snapshot FIRST so the wholesale working-copy replacement is reversible (ADR-004).
    pre = create_snapshot(doc_id, label="pre-reload", registry=reg)

    try:
        reg.reload(doc_id)
    except KeyError as exc:  # pragma: no cover - existence checked above
        raise ToolError("document id not found") from exc
    except SandboxViolation as exc:
        _logger.error("reload_document rejected", extra={"detail": exc.detail})
        raise ToolError(str(exc)) from exc
    except LimitExceeded as exc:
        raise ToolError("input file exceeds the configured size limit") from exc

    log_tool_call(
        _logger, tool="reload_document", doc_id=doc_id, pre_reload_snapshot_id=pre.snapshot_id
    )
    summary = inspect_summary(doc_id)
    return ReloadDocumentResult(
        doc_id=doc_id, pre_reload_snapshot_id=pre.snapshot_id, summary=summary
    )


@mcp.tool
def inspect_document(doc_id: str) -> InspectDocumentResult:
    """Inspect a loaded document: tree, layers, styles, fonts, external assets.

    When to use: the go-to overview to understand a document's structure AND discover targetable
    object ids before editing. To search for SPECIFIC objects by filter use `find_objects`; for
    quality metrics use `quality_report`; for well-formedness use `validate_document`.

    Key params: `doc_id` only (read-only).

    Return shape: `InspectDocumentResult` — `summary`, `tree`, `layers`, `styles`, `fonts`,
    `assets`, and `objects` (flat list of every id-bearing object with tag / bbox / paint / text,
    the same `ObjectRef` shape `find_objects` returns).

    Example: `inspect_document(doc_id)`

    Risk class: low (read-only, direct DOM per ADR-005).
    """
    try:
        result = InspectDocumentResult(
            summary=inspect_summary(doc_id),
            tree=inspect_tree(doc_id),
            layers=inspect_layers(doc_id),
            styles=inspect_styles(doc_id),
            fonts=inspect_fonts(doc_id),
            assets=inspect_assets(doc_id),
            objects=list_objects(doc_id),
        )
    except DocumentNotFound as exc:
        raise ToolError("document id not found") from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc

    log_tool_call(_logger, tool="inspect_document", doc_id=doc_id)
    return result
