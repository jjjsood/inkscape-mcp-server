"""`render`: look at the page, one object or the next frame of a series as an inline PNG."""

import re
from pathlib import Path
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
from inkscape_mcp.render.inline import resolve_artifact_abspath
from inkscape_mcp.tools._result import guarded, inline_png, ok, require_doc

_logger = get_logger("tools.render")

Target = Literal["page", "object", "frame"]

DESCRIPTION = (
    "Render the document as a PNG you can see inline. target='page' (default): the whole page; "
    "'object': a zoomed view of one object, clipped to its box (needs object_id; to view, "
    "not resize); 'frame': the next numbered frame of a `series` (a screenshot log; optional "
    "`label`). width_px sets the raster width. background (CSS colour such as '#333', "
    "default none = transparent) fills the transparent areas, e.g. to see white shapes. Use it "
    "after edits to check the result (edit(render='inline') already includes one render). "
    "Example: render(doc_id, target='object', object_id='logo', width_px=512)"
)


_COLOUR_RE = re.compile(r"^(#[0-9a-fA-F]{3,4}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8}|[a-zA-Z]{3,20})$")
_NEAR_WHITE = 235
_LOW_CONTRAST_NOTE = (
    "drawn pixels are nearly all white on a transparent background, so viewers may show it blank; "
    "re-render with background='#333'"
)


def _colour(value: str) -> str:
    from PIL import ImageColor

    try:
        if not _COLOUR_RE.fullmatch(value):
            raise ValueError(value)
        ImageColor.getrgb(value)
        return value
    except ValueError:
        raise ToolError(
            "background must be a CSS colour like '#333', '#1e1e1e' or 'black'"
        ) from None


def _apply_background(path: Path, colour: str | None) -> str | None:
    """Composite the PNG onto `colour` in place; with no colour, return a low-contrast note.

    The note fires when at least 95% of the drawn (non-transparent) pixels are near-white.
    """
    from PIL import Image, ImageChops

    try:
        with Image.open(path) as opened:
            im = opened.convert("RGBA")
        if colour is not None:
            canvas = Image.new("RGBA", im.size, colour)
            canvas.alpha_composite(im)
            canvas.save(path, format="PNG")
            return None
        r, g, b, a = im.split()
        drawn = a.point(lambda v: 255 if v else 0)
        n_drawn = drawn.histogram()[255]
        white = ImageChops.darker(
            ImageChops.darker(ImageChops.darker(r, g), b).point(
                lambda v: 255 if v >= _NEAR_WHITE else 0
            ),
            drawn,
        )
        if n_drawn and white.histogram()[255] >= 0.95 * n_drawn:
            return _LOW_CONTRAST_NOTE
    except OSError:
        pass
    return None


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
    background: str | None = None,
) -> ToolResult:
    require_doc(doc_id)
    colour = _colour(background) if background is not None else None
    if target != "object" and object_id is not None:
        raise ToolError("object_id is only used with target='object'")
    if target != "frame" and (series is not None or label is not None):
        raise ToolError("series and label are only used with target='frame'")

    extra: dict[str, object] = {}
    if target == "page":
        result: RenderResult = render_preview(doc_id, width_px=width_px)
    elif target == "object":
        if not object_id:
            raise ToolError(
                "target='object' requires object_id; call inspect(include=['objects']) for ids"
            )
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
    payload = {"doc_id": doc_id, "target": target, **_info(result), **extra}
    artifact = resolve_artifact_abspath(doc_id, result.workspace_relative_path)
    if artifact is not None and (hint := _apply_background(artifact, colour)):
        payload["note"] = f"{payload['note']}; {hint}"
    image = inline_png(doc_id, result.workspace_relative_path)
    if image is None:
        payload["note"] = "image too large to inline; lower width_px"
    return ok(payload, image)


def register(mcp: FastMCP) -> None:
    mcp.tool(name="render", description=DESCRIPTION, output_schema=None)(render_)
