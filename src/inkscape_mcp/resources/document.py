"""Document inspection resources.

Publishes the inspection views as read-only MCP resource templates so hosts can pull
structured document state without a tool round-trip. Each resource is a thin wrapper over the
reusable inspection engine (`inkscape_mcp.document.inspect`); for a given `doc_id` the payload
is IDENTICAL to the matching component of the `inspect_document` tool result (parity contract).

The `{doc_id}` URI placeholder binds to each function's `doc_id` parameter, so FastMCP registers
these as resource TEMPLATES. Library-level errors are mapped to client-visible `ResourceError`s
with stable, host-path-free messages (fastmcp-patterns error model / sec.12): never let a
`DocumentNotFound` / `InspectionError` (or any internal detail) leak through unmasked.
"""

from __future__ import annotations

import json

from fastmcp.exceptions import ResourceError

from inkscape_mcp.document.inspect import (
    DocumentNotFound,
    InspectionError,
    inspect_assets,
    inspect_fonts,
    inspect_layers,
    inspect_objects,
    inspect_styles,
    inspect_summary,
    inspect_tree,
)
from inkscape_mcp.registry import get_registry
from inkscape_mcp.server import mcp

_NOT_FOUND = "document id not found"
_PARSE_FAILED = "document could not be parsed safely"

# The per-document resource leaves published as templates below. The `inkscape://documents` index
# uses this list to emit the concrete per-doc URIs, so the index and the templates never drift.
_DOC_RESOURCE_LEAVES = ("summary", "tree", "layers", "objects", "styles", "fonts", "assets")


@mcp.resource("inkscape://document/{doc_id}/summary", mime_type="application/json")
def document_summary(doc_id: str) -> str:
    """Document summary (size, units, viewBox, page/object/layer counts) for one document.

    Risk class: low (read-only resource).
    """
    try:
        return inspect_summary(doc_id).model_dump_json()
    except DocumentNotFound as exc:
        raise ResourceError(_NOT_FOUND) from exc
    except InspectionError as exc:
        raise ResourceError(_PARSE_FAILED) from exc


@mcp.resource("inkscape://document/{doc_id}/tree", mime_type="application/json")
def document_tree(doc_id: str) -> str:
    """Full element tree (local tags + ids + inkscape:labels) for one document.

    Risk class: low (read-only resource).
    """
    try:
        return inspect_tree(doc_id).model_dump_json()
    except DocumentNotFound as exc:
        raise ResourceError(_NOT_FOUND) from exc
    except InspectionError as exc:
        raise ResourceError(_PARSE_FAILED) from exc


@mcp.resource("inkscape://document/{doc_id}/layers", mime_type="application/json")
def document_layers(doc_id: str) -> str:
    """All Inkscape layers (with visible/locked state) for one document.

    Risk class: low (read-only resource).
    """
    try:
        return inspect_layers(doc_id).model_dump_json()
    except DocumentNotFound as exc:
        raise ResourceError(_NOT_FOUND) from exc
    except InspectionError as exc:
        raise ResourceError(_PARSE_FAILED) from exc


@mcp.resource("inkscape://document/{doc_id}/objects", mime_type="application/json")
def document_objects(doc_id: str) -> str:
    """Flattened object list for one document.

    Risk class: low (read-only resource).
    """
    try:
        return inspect_objects(doc_id).model_dump_json()
    except DocumentNotFound as exc:
        raise ResourceError(_NOT_FOUND) from exc
    except InspectionError as exc:
        raise ResourceError(_PARSE_FAILED) from exc


@mcp.resource("inkscape://document/{doc_id}/styles", mime_type="application/json")
def document_styles(doc_id: str) -> str:
    """Distinct fill/stroke colors plus inline-style and CSS-rule counts for one document.

    Risk class: low (read-only resource).
    """
    try:
        return inspect_styles(doc_id).model_dump_json()
    except DocumentNotFound as exc:
        raise ResourceError(_NOT_FOUND) from exc
    except InspectionError as exc:
        raise ResourceError(_PARSE_FAILED) from exc


@mcp.resource("inkscape://document/{doc_id}/fonts", mime_type="application/json")
def document_fonts(doc_id: str) -> str:
    """All font-family values (inline styles, `<style>` bodies, attrs) for one document.

    Risk class: low (read-only resource).
    """
    try:
        return inspect_fonts(doc_id).model_dump_json()
    except DocumentNotFound as exc:
        raise ResourceError(_NOT_FOUND) from exc
    except InspectionError as exc:
        raise ResourceError(_PARSE_FAILED) from exc


@mcp.resource("inkscape://document/{doc_id}/assets", mime_type="application/json")
def document_assets(doc_id: str) -> str:
    """Referenced assets (images, uses, external url() refs) for one document.

    Risk class: low (read-only resource).
    """
    try:
        return inspect_assets(doc_id).model_dump_json()
    except DocumentNotFound as exc:
        raise ResourceError(_NOT_FOUND) from exc
    except InspectionError as exc:
        raise ResourceError(_PARSE_FAILED) from exc


@mcp.resource("inkscape://documents", mime_type="application/json")
def documents_index() -> str:
    """Index of open documents and their per-document resource URIs (discoverability).

    The seven per-document resources above are registered as URI TEMPLATES (they carry a `{doc_id}`
    placeholder), so they appear under `resources/templates/list` but NOT under the plain
    `resources/list` that `ListMcpResourcesTool` reads — an agent could not otherwise discover the
    concrete URIs for an opened document. This static (no-placeholder) resource closes that gap: it
    lists every currently-open document id with the seven concrete `inkscape://document/<id>/<leaf>`
    URIs for it, so the per-doc read surface is discoverable through `ListMcpResourcesTool`.

    Host-path-free: only opaque document ids and their resource URIs are emitted — never a working
    path. Risk class: low (read-only resource).
    """
    documents = [
        {
            "doc_id": entry.doc_id,
            "resources": {
                leaf: f"inkscape://document/{entry.doc_id}/{leaf}" for leaf in _DOC_RESOURCE_LEAVES
            },
        }
        for entry in get_registry().list_documents()
    ]
    return json.dumps({"documents": documents})
