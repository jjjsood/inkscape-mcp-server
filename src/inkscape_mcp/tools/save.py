"""Save tool: `save_document_as` (thin MCP layer over :mod:`inkscape_mcp.document.save`)."""

from __future__ import annotations

from inkscape_mcp.document.save import SaveResult
from inkscape_mcp.document.save import save_document_as as _save_document_as
from inkscape_mcp.server import mcp

__all__ = ["SaveResult", "save_document_as"]


@mcp.tool
def save_document_as(
    doc_id: str,
    dest_path: str,
    overwrite: bool = False,
    approval_token: str | None = None,
) -> SaveResult:
    """Save a document's current working-copy state to a NEW file in the workspace.

    When to use: persisting the working copy to disk. To export a raster/PDF instead use
    `export_document`; to snapshot in-server state (not a file) use `create_snapshot`. The original
    and source files are never touched.

    Key params: `dest_path` may be RELATIVE or absolute — relative anchors to the FIRST configured
    workspace root (NOT the server CWD); absolute must resolve inside a configured root. A dest into
    a not-yet-existing SUBFOLDER (e.g. `"output/final.svg"`) is supported: missing parents are
    created only after proving they resolve INSIDE the workspace (a `..`-escaping / out-of-sandbox
    dest creates nothing and is rejected with `path rejected: outside workspace`). The dest is
    sandbox- and symlink-checked (incl. a pre-existing symlink at the final name) and the copy never
    follows a symlinked dest (sec.12). Overwriting an existing file requires `overwrite=True` PLUS a
    non-empty `approval_token`.

    Return shape: `SaveResult` — `saved_path` (workspace-relative POSIX), `operation_id`,
    `overwritten`, and `pre_validation` / `post_validation` (the `validate_document` reports from
    before and after the write).

    Example: `save_document_as(doc_id, "output/final.svg")`

    Risk class: medium for a new-file save; high (approval-gated) when overwriting an existing file.
    """
    return _save_document_as(
        doc_id=doc_id, dest_path=dest_path, overwrite=overwrite, approval_token=approval_token
    )
