"""`live`: observe and steer a running Inkscape (read-only; changes go through `live_edit`).

The session connects on demand: any observing action attaches to the best transport first.
"""

from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult

from inkscape_mcp.live import admin
from inkscape_mcp.live.diff import diff_live_operation
from inkscape_mcp.live.edit import export_live_selection, set_live_viewport, validate_viewport
from inkscape_mcp.live.events import (
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_WAIT_TIMEOUT_S,
    wait_for_change,
)
from inkscape_mcp.live.render import render_live_view
from inkscape_mcp.live.scene import get_live_scene
from inkscape_mcp.live.session import get_session_manager
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.tools._live import Region, ensure_connected, frame_params, live_png
from inkscape_mcp.tools._result import compact, guarded, ok

_logger = get_logger("tools.live")

Action = Literal[
    "check",
    "connect",
    "disconnect",
    "status",
    "install_helper",
    "arm_socket",
    "observe",
    "wait",
    "viewport",
    "diff",
    "export_selection",
]
Part = Literal["document", "selection", "inspect", "scene", "view"]
Prefer = Literal["read", "no_freeze"]

DESCRIPTION = (
    "Look at (and steer the view of) a running Inkscape. action='observe' with include=[document, "
    "selection, inspect, scene, view] returns those parts in one call ('view' is an inline PNG of "
    "the canvas; region/scale/fast shape it); 'wait' blocks until something changes; 'viewport' "
    "(mode, zoom, center_x/y, dx/dy) moves the view; 'diff' (operation_id) shows what a live_edit "
    "changed; 'export_selection' renders the selection. Setup: check, connect, disconnect, "
    "status, install_helper, arm_socket. Connects automatically. "
    "Example: live(action='observe', include=['selection','inspect','view'], fast=true)"
)


def _observe(
    include: list[Part], region: Region | None, scale: float | None, fast: bool
) -> tuple[dict[str, Any], list[Any]]:
    manager = ensure_connected()
    transport = manager.require_transport()
    checked_region, checked_scale = frame_params(region, scale, fast)
    out: dict[str, Any] = {}
    images: list[Any] = []
    if "document" in include:
        out["document"] = transport.get_active_document()
    if "selection" in include:
        out["selection"] = transport.get_selection()
    if "inspect" in include:
        out["inspect"] = transport.inspect_selection()
    if "scene" in include:
        out["scene"] = get_live_scene()
    if "view" in include:
        frame = render_live_view(region=checked_region, scale=checked_scale)  # type: ignore[arg-type]
        out["view"] = {"scale": frame.scale, "region": frame.region or None}
        images.append(live_png(frame.artifact_path))
    return out, images


@guarded()
def live(
    action: Action,
    prefer: Prefer = "read",
    include: list[Part] | None = None,
    region: Region | None = None,
    scale: float | None = None,
    fast: bool = False,
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    mode: str | None = None,
    zoom: float | None = None,
    center_x: float | None = None,
    center_y: float | None = None,
    dx: float | None = None,
    dy: float | None = None,
    operation_id: str | None = None,
) -> ToolResult:
    images: list[Any] = []
    payload: Any
    if action == "check":
        payload = admin.check_live_support()
    elif action == "status":
        payload = get_session_manager().status()
    elif action == "connect":
        payload = ensure_connected(prefer).status()
    elif action == "disconnect":
        payload = get_session_manager().disconnect()
    elif action == "install_helper":
        payload = admin.live_install_helper()
    elif action == "arm_socket":
        payload = admin.live_arm_socket()
    elif action == "observe":
        payload, images = _observe(include or ["selection"], region, scale, fast)
    elif action == "wait":
        if not 0.0 <= timeout_s <= 60.0:
            raise ToolError("timeout_s must be between 0 and 60")
        if not 0.0 < poll_interval_s <= 60.0:
            raise ToolError("poll_interval_s must be > 0 and <= 60")
        ensure_connected()
        payload = wait_for_change(timeout_s=timeout_s, poll_interval_s=poll_interval_s)
    elif action == "viewport":
        if mode is None:
            raise ToolError("action='viewport' requires mode (zoom, pan, fit_selection, fit_page)")
        ensure_connected()
        kwargs = validate_viewport(
            mode=mode, zoom=zoom, center_x=center_x, center_y=center_y, dx=dx, dy=dy
        )
        payload = set_live_viewport(kwargs)
    elif action == "diff":
        if not operation_id:
            raise ToolError("action='diff' requires operation_id (from a live_edit result)")
        diff = diff_live_operation(operation_id)
        images.append(live_png(diff.artifact_path))
        payload = diff
    else:  # export_selection
        ensure_connected()
        exported = export_live_selection()
        images.append(live_png(exported.artifact_path))
        payload = exported

    log_tool_call(_logger, tool="live", action=action)
    return ok(_strip_paths(payload, images), *images)


def _strip_paths(payload: Any, images: list[Any]) -> Any:
    """Drop server-side artifact paths when the image itself is attached."""
    if not any(i is not None for i in images):
        return payload
    data = compact(payload)
    if isinstance(data, dict):
        data.pop("artifact_path", None)
    return data


def register(mcp: FastMCP) -> None:
    mcp.tool(name="live", description=DESCRIPTION, output_schema=None)(live)
