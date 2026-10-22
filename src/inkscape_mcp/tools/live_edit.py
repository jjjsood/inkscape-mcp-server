"""`live_edit`: change the selection (or insert content) in a running Inkscape, then observe."""

from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from pydantic import BaseModel, ConfigDict

from inkscape_mcp.edit.dom import EditError
from inkscape_mcp.live.diff import diff_live_operation
from inkscape_mcp.live.edit import (
    build_style,
    build_transform,
    run_live_mutation,
    validate_svg_fragment,
    validate_text,
)
from inkscape_mcp.live.protocol import LiveCommand
from inkscape_mcp.live.render import render_live_view
from inkscape_mcp.live.transport import LiveMutationResult, LiveTransport
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.tools._live import ensure_connected, live_png
from inkscape_mcp.tools._result import guarded, ok

_logger = get_logger("tools.live_edit")

Action = Literal["apply", "insert_svg", "set_text"]
Observe = Literal["none", "diff", "render"]

DESCRIPTION = (
    "Change the live Inkscape selection. action='apply' with style {fill, stroke, stroke_width, "
    "opacity} and/or transform {dx, dy, scale, rotate}; 'insert_svg' with svg_fragment; "
    "'set_text' with text. observe='render' returns an inline PNG of the canvas afterwards, "
    "'diff' a before/after overlay, 'none' just the result. Always HIGH risk: needs "
    "approval_token. Connects automatically. Example: live_edit(action='apply', "
    "style={'fill':'#f63'}, observe='render', approval_token='ok')"
)


class Style(BaseModel):
    """Style properties to set on the selection."""

    model_config = ConfigDict(extra="forbid")

    fill: str | None = None
    stroke: str | None = None
    stroke_width: str | None = None
    opacity: float | None = None


class Transform(BaseModel):
    """Move (dx, dy), scale and rotate (degrees) the selection."""

    model_config = ConfigDict(extra="forbid")

    dx: float | None = None
    dy: float | None = None
    scale: float | None = None
    rotate: float | None = None


def _plan(
    action: Action,
    style: Style | None,
    transform: Transform | None,
    svg_fragment: str | None,
    text: str | None,
) -> tuple[str, dict[str, object], LiveCommand, Any]:
    """Validate the params for `action`; return (record name, record params, command, op)."""
    if action == "apply":
        if svg_fragment is not None or text is not None:
            raise ToolError("action='apply' takes style and/or transform only")
        built_style = build_style(**style.model_dump()) if style is not None else {}
        built_transform = (
            build_transform(**transform.model_dump()) if transform is not None else None
        )
        if not built_style and built_transform is None:
            raise EditError("supply at least one style or transform property")

        def apply_op(t: LiveTransport) -> LiveMutationResult:
            return t.apply_to_selection(style=built_style, transform=built_transform)

        return (
            "live_edit.apply",
            {"style": built_style, "transform": built_transform},
            LiveCommand.APPLY_TO_SELECTION,
            apply_op,
        )
    if style is not None or transform is not None:
        raise ToolError(f"action={action!r} does not take style or transform")
    if action == "insert_svg":
        if svg_fragment is None or text is not None:
            raise ToolError("action='insert_svg' requires svg_fragment (and no text)")
        fragment = validate_svg_fragment(svg_fragment)

        def insert_op(t: LiveTransport) -> LiveMutationResult:
            return t.insert_svg(fragment)

        return (
            "live_edit.insert_svg",
            {"fragment_bytes": len(fragment.encode("utf-8"))},
            LiveCommand.INSERT_SVG,
            insert_op,
        )
    if text is None or svg_fragment is not None:
        raise ToolError("action='set_text' requires text (and no svg_fragment)")
    safe_text = validate_text(text)

    def text_op(t: LiveTransport) -> LiveMutationResult:
        return t.set_selected_text(safe_text)

    return (
        "live_edit.set_text",
        {"text_len": len(safe_text)},
        LiveCommand.SET_SELECTED_TEXT,
        text_op,
    )


@guarded()
def live_edit(
    action: Action,
    style: Style | None = None,
    transform: Transform | None = None,
    svg_fragment: str | None = None,
    text: str | None = None,
    observe: Observe = "none",
    approval_token: str | None = None,
) -> ToolResult:
    tool, params, command, op = _plan(action, style, transform, svg_fragment, text)
    ensure_connected()
    result = run_live_mutation(
        tool=tool,
        params=params,
        required_command=command,
        op=op,
        approval_token=approval_token,
    )
    payload: dict[str, Any] = {
        "operation_id": result.operation_id,
        "summary": result.summary,
        "affected_ids": result.affected_ids,
        "count": result.count,
        "undo_friendly": result.undo_friendly or None,
    }
    images: list[Any] = []
    if observe == "render":
        try:
            images.append(live_png(render_live_view().artifact_path))
        except Exception:
            payload["warning"] = "render unavailable"
    elif observe == "diff":
        try:
            diff = diff_live_operation(result.operation_id)
            images.append(live_png(diff.artifact_path))
            payload["changed_bbox"] = diff.changed_bbox
            payload["highlighted_ids"] = diff.highlighted_ids
        except Exception:
            payload["warning"] = "diff unavailable"
    log_tool_call(_logger, tool="live_edit", action=action, observe=observe)
    return ok(payload, *images)


def register(mcp: FastMCP) -> None:
    mcp.tool(name="live_edit", description=DESCRIPTION, output_schema=None)(live_edit)
