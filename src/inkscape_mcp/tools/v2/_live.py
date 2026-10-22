"""Shared helpers for the `live`, `live_edit` and `open(from_live=...)` tools."""

from pathlib import Path

from fastmcp.utilities.types import Image
from pydantic import BaseModel, ConfigDict

from inkscape_mcp.config import get_settings
from inkscape_mcp.edit.dom import EditError
from inkscape_mcp.live.edit import FAST_RENDER_SCALE, validate_region, validate_render_scale
from inkscape_mcp.live.session import LiveSessionManager, get_session_manager
from inkscape_mcp.render.inline import DEFAULT_INLINE_MAX_BYTES


class Region(BaseModel):
    """A canvas region in user units: origin plus a positive extent."""

    model_config = ConfigDict(extra="forbid")

    x: float
    y: float
    width: float
    height: float


def ensure_connected(prefer: str = "read") -> LiveSessionManager:
    """The session manager with a transport attached, connecting first when needed.

    `connect` tears down an existing session, so a live session is reused, never reconnected.
    """
    if prefer not in ("read", "no_freeze"):
        raise EditError("prefer must be 'read' or 'no_freeze'")
    manager = get_session_manager()
    if not manager.status().connected:
        manager.connect(prefer=prefer)
    return manager


def frame_params(
    region: Region | None, scale: float | None, fast: bool
) -> tuple[object | None, float | None]:
    """Validate region / scale / fast; an explicit `scale` wins over the `fast` downscale."""
    checked_region = (
        validate_region(region.x, region.y, region.width, region.height)
        if region is not None
        else None
    )
    effective = scale if scale is not None else (FAST_RENDER_SCALE if fast else None)
    checked_scale = validate_render_scale(effective) if effective is not None else None
    return checked_region, checked_scale


def live_png(workspace_relative_path: str | None) -> Image | None:
    """A server-minted live artifact (workspace-root-relative PNG) as an inline image, else None."""
    if not workspace_relative_path:
        return None
    settings = get_settings()
    if not settings.workspace_roots:
        return None
    root = settings.workspace_roots[0].resolve()
    candidate = (root / workspace_relative_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    limit = min(DEFAULT_INLINE_MAX_BYTES, settings.max_output_bytes)
    try:
        if candidate.stat().st_size > limit:
            return None
        return Image(data=Path(candidate).read_bytes(), format="png")
    except OSError:
        return None
