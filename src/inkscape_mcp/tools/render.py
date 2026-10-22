"""`render`: look at the page, one object or the next frame of a series as an inline PNG."""

from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult

from inkscape_mcp.document.inspect import inspect_objects
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.render.cli import (
    RenderResult,
    capture_frame,
    export_object,
    is_safe_object_id,
    render_preview,
)
from inkscape_mcp.tools._result import guarded, inline_png, ok, require_doc

_logger = get_logger("tools.render")

Target = Literal["page", "object", "frame"]

DESCRIPTION = (
    "Render the document as a PNG you can see inline. target='page' (default): the whole page; "
    "'object': one object clipped to its box (needs object_id); 'frame': the next numbered frame "
    "of a `series` (a screenshot log; optional `label`). width_px sets the raster width. Use it "
    "after edits to check the result (edit(render='inline') already includes one render). "
    "Example: render(doc_id, target='object', object_id='logo', width_px=512)"
)


def _info(result: RenderResult) -> dict[str, object]:
    return {
        "width_px": result.width_px,
        "height_px": result.height_px,
        "stale": result.stale or None,
        "all_blank": result.all_blank,
        "note": "PNG attached inline; the artifact is server-side, do not Read it",
    }


@guarded(limit_msg="render exceeds the configured size or dimension limit")
def render_(
    doc_id: str,
    target: Target = "page",
    object_id: str | None = None,
    width_px: int | None = None,
    series: str | None = None,
    label: str | None = None,
) -> ToolResult:
    require_doc(doc_id)
    if target != "object" and object_id is not None:
        raise ToolError("object_id is only used with target='object'")
    if target != "frame" and (series is not None or label is not None):
        raise ToolError("series and label are only used with target='frame'")

    extra: dict[str, object] = {}
    if target == "page":
        result: RenderResult = render_preview(doc_id, width_px=width_px)
    elif target == "object":
        if not object_id:
            raise ToolError("target='object' requires object_id")
        if not is_safe_object_id(object_id):
            raise ToolError("object id is not valid")
        if not any(o.id == object_id for o in inspect_objects(doc_id).objects):
            raise ToolError(
                "object id not found in document; call inspect(include=['objects']) for ids"
            )
        result = export_object(doc_id, object_id, "png", width_px=width_px)
    else:
        frame = capture_frame(doc_id, series=series, width_px=width_px, label=label)
        result = frame
        extra = {"series": frame.series, "frame": frame.frame_index}

    log_tool_call(_logger, tool="render", doc_id=doc_id, target=target)
    image = inline_png(doc_id, result.workspace_relative_path or result.artifact_path)
    payload = {"doc_id": doc_id, "target": target, **_info(result), **extra}
    if image is None:
        payload["note"] = "image too large to inline; lower width_px"
    return ok(payload, image)


def register(mcp: FastMCP) -> None:
    mcp.tool(name="render", description=DESCRIPTION, output_schema=None)(render_)
