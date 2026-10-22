"""`open`: get a document id from a file, svg text, a blank canvas or the live canvas, optionally
running the first ops in the same call.
"""

from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from lxml import etree
from pydantic import BaseModel, ConfigDict

from inkscape_mcp.edit.compose import build_blank_svg, parse_and_scrub
from inkscape_mcp.edit.ops import run_ops
from inkscape_mcp.live.sync import sync_live_to_workspace
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.registry import get_registry
from inkscape_mcp.tools.v2._live import ensure_connected
from inkscape_mcp.tools.v2._result import doc_summary, guarded, inline_png, ok, to_tool_error
from inkscape_mcp.tools.v2.edit import Detail, ops_payload

_logger = get_logger("tools.v2.open")

PERSIST_HINT = (
    "Edits go to a managed working copy; your file is never changed. Use "
    "export(targets=[{'kind':'save_as','dest_path':...}]) to write a file."
)

DESCRIPTION = (
    "Start working: returns a doc_id. Give exactly ONE source: path (existing SVG in the "
    "workspace), svg (SVG text), canvas {width,height,viewBox?,background?} (blank page) or "
    "from_live {dest_path} (save the live Inkscape canvas into the workspace). Optional `ops` run "
    "the first edit in the same call (same format as edit; render='inline' returns a PNG). "
    "Example: open(canvas={'width':800,'height':600,'background':'#fff'}, "
    "ops=[{'op':'create_circle','cx':400,'cy':300,'r':100,'fill':'#f63'}], render='inline')"
)


class Canvas(BaseModel):
    """A blank page: size in user units, optional `viewBox` ("minx miny w h") and background."""

    model_config = ConfigDict(extra="forbid")

    width: float
    height: float
    viewBox: str | None = None
    background: str | None = None


class FromLive(BaseModel):
    """Save the live canvas to `dest_path` (workspace-relative) and open that copy."""

    model_config = ConfigDict(extra="forbid")

    dest_path: str


def _scrubbed_bytes(svg: str) -> bytes:
    root = parse_and_scrub(svg)
    if etree.QName(root).localname != "svg":
        raise ToolError("svg must have an <svg> root element")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


@guarded(limit_msg="input exceeds the configured size limit")
def open_(
    path: str | None = None,
    svg: str | None = None,
    canvas: Canvas | None = None,
    from_live: FromLive | None = None,
    ops: list[dict[str, Any]] | None = None,
    render: Literal["none", "inline"] = "none",
    detail: Detail = "ids",
    approval_token: str | None = None,
) -> ToolResult:
    given = [
        n
        for n, v in (("path", path), ("svg", svg), ("canvas", canvas), ("from_live", from_live))
        if v is not None
    ]
    if len(given) != 1:
        raise ToolError(
            "give exactly one source: path, svg, canvas or from_live "
            f"(got {', '.join(given) if given else 'none'})"
        )
    if render == "inline" and not ops:
        raise ToolError("render='inline' needs ops; use render(doc_id) to look at the document")

    reg = get_registry()
    payload: dict[str, Any] = {}
    if path is not None:
        doc_id = reg.open_document(path).doc_id
    elif svg is not None:
        doc_id = reg.create_document(_scrubbed_bytes(svg)).doc_id
    elif canvas is not None:
        blank = build_blank_svg(
            canvas.width, canvas.height, viewbox=canvas.viewBox, background=canvas.background
        )
        doc_id = reg.create_document(blank).doc_id
    else:
        ensure_connected()
        synced = sync_live_to_workspace(from_live.dest_path)  # type: ignore[union-attr]
        doc_id = synced.doc_id
        payload["saved_path"] = synced.saved_path

    payload.update(doc_id=doc_id, summary=doc_summary(doc_id), persist_hint=PERSIST_HINT)
    log_tool_call(_logger, tool="open", doc_id=doc_id, source=given[0], ops=len(ops or []))
    if not ops:
        return ok(payload)

    try:
        res = run_ops(
            doc_id,
            ops,
            render=render == "inline",
            approval_token=approval_token,
            tool="open",
        )
    except Exception as exc:
        # The document exists already; say so, so the caller can retry with `edit`.
        error = to_tool_error(exc)
        raise ToolError(f"opened {doc_id}, but ops failed: {error}") from exc
    payload.update(ops_payload(res, detail))
    return ok(payload, inline_png(doc_id, res.render_artifact))


def register(mcp: FastMCP) -> None:
    mcp.tool(name="open", description=DESCRIPTION, output_schema=None)(open_)
