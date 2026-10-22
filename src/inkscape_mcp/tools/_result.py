"""Shared result builder and error map for the result-oriented tool surface.

Every tool returns ``ToolResult(content=[TextContent(compact_json)] (+ ImageContent))`` and never
sets ``structured_content``: with ``output_schema=None`` that keeps ``outputSchema`` off
``tools/list`` and stops FastMCP from sending the payload twice.

``to_tool_error`` is the ONE exception -> ``ToolError`` map for the whole surface (it replaces the
per-module copies of the old tools). All messages are stable and host-path-free (sec.12).
"""

import functools
import json
from collections.abc import Callable
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from mcp.types import TextContent
from pydantic import BaseModel

from inkscape_mcp.actions.chains import ActionChainError, ActionEngineError
from inkscape_mcp.document.inspect import DocumentNotFound, InspectionError
from inkscape_mcp.edit.compose import ComposeError
from inkscape_mcp.edit.dom import EditError, TargetNotFound
from inkscape_mcp.edit.paths import PathOpError
from inkscape_mcp.edit.pipeline import EditApplyError
from inkscape_mcp.live.diff import LiveDiffError
from inkscape_mcp.live.sync import LiveSyncError
from inkscape_mcp.live.transport import (
    LiveCapabilityUnsupported,
    LiveConnectionError,
    LiveDisabled,
    LiveError,
    LiveNotAvailable,
)
from inkscape_mcp.logging_setup import get_logger
from inkscape_mcp.render.batch import BatchError
from inkscape_mcp.render.cli import InvalidObjectId, RenderError
from inkscape_mcp.render.inline import inline_image
from inkscape_mcp.render.profiles import ProfileSizeError
from inkscape_mcp.snapshots import SnapshotNotFound
from inkscape_mcp.workspace.limits import LimitExceeded
from inkscape_mcp.workspace.paths import SandboxViolation
from inkscape_mcp.workspace.risk import PolicyViolation
from inkscape_mcp.workspace.subprocess_exec import ProcessError
from inkscape_mcp.workspace.xml_safety import UnsafeXMLError

_logger = get_logger("tools")

DEFAULT_LIMIT_MSG = "input exceeds the configured size or dimension limit"
_ENGINE_UNAVAILABLE = "inkscape engine unavailable"
_ENGINE_HINT = (
    "the Inkscape engine is unavailable on this runtime; call help(topic='diagnose') to see "
    "what is installed"
)

#: Exceptions whose message is already built from typed parameters (stable, host-path-free).
_PASSTHROUGH = (
    EditError,  # also OpError / ComposeError
    ComposeError,
    PolicyViolation,
    SandboxViolation,
    BatchError,
    ProfileSizeError,
    PathOpError,
    LiveSyncError,
    LiveDiffError,
)


def _live_error(exc: LiveError) -> ToolError:
    """Live-layer failures, worded for the `live` / `live_edit` tools."""
    if isinstance(exc, LiveDisabled):
        return ToolError(
            "live mode is disabled (set INKSCAPE_MCP_LIVE_ENABLED=1 to enable); "
            "call live(action='check') to inspect live readiness"
        )
    if isinstance(exc, LiveNotAvailable):
        return ToolError(
            "no live session available; call live(action='connect') "
            "(or live(action='check') to see what this host supports)"
        )
    if isinstance(exc, LiveCapabilityUnsupported):
        return ToolError(
            "the active live transport does not support this operation; "
            "call live(action='check') to see which transport does"
        )
    if isinstance(exc, LiveConnectionError):
        return ToolError("live session communication failed")
    return ToolError("live operation failed")


def to_tool_error(exc: BaseException, *, limit_msg: str = DEFAULT_LIMIT_MSG) -> ToolError:
    """Map any engine exception to a stable, host-path-free, actionable `ToolError`."""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, KeyError | DocumentNotFound | EditApplyError):
        return ToolError("document id not found")
    if isinstance(exc, TargetNotFound):
        return ToolError("object id not found in document")
    if isinstance(exc, InspectionError | UnsafeXMLError):
        return ToolError("document could not be parsed safely")
    if isinstance(exc, _PASSTHROUGH):
        return ToolError(str(exc))
    if isinstance(exc, LimitExceeded):
        return ToolError(limit_msg)
    if isinstance(exc, ProcessError):
        return ToolError(f"render/export failed: {_ENGINE_HINT}")
    if isinstance(exc, RenderError):
        return ToolError("render/export failed")
    if isinstance(exc, InvalidObjectId):
        return ToolError("object id is not valid")
    if isinstance(exc, SnapshotNotFound):
        return ToolError("snapshot not found; call history(action='list') for valid ids")
    if isinstance(exc, ActionChainError):
        message = f"{exc.code}: {exc.message}"
        if exc.code == "action_absent":
            message += "; call actions(action='list') to see which Actions this runtime exposes"
        return ToolError(message)
    if isinstance(exc, ActionEngineError):
        if str(exc) == _ENGINE_UNAVAILABLE:
            return ToolError(f"{_ENGINE_UNAVAILABLE} on this runtime; call help(topic='diagnose')")
        return ToolError(str(exc))
    if isinstance(exc, LiveError):
        return _live_error(exc)
    _logger.error("unexpected tool failure", extra={"detail": type(exc).__name__})
    return ToolError("operation failed")


def guarded[**P, R](
    *, limit_msg: str = DEFAULT_LIMIT_MSG
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator: run the tool body and fold every failure into `to_tool_error`."""

    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return fn(*args, **kwargs)
            except ToolError:
                raise
            except Exception as exc:
                raise to_tool_error(exc, limit_msg=limit_msg) from exc

        return wrapper

    return decorate


def compact(value: Any) -> Any:
    """Drop `None`, empty strings and empty containers recursively; `False` and `0` survive."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        out = {k: compact(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v is not None and v != [] and v != {} and v != ""}
    if isinstance(value, list | tuple):
        return [compact(v) for v in value]
    return value


def dumps(payload: Any) -> str:
    """Compact JSON text."""
    return json.dumps(compact(payload), separators=(",", ":"), ensure_ascii=False)


def ok(payload: Any, *images: Image | None) -> ToolResult:
    """Build the tool result: one compact JSON text block plus any inline images."""
    content: list[Any] = [TextContent(type="text", text=dumps(payload))]
    content.extend(img.to_image_content() for img in images if img is not None)
    return ToolResult(content=content)


def inline_png(doc_id: str, workspace_relative_path: str | None) -> Image | None:
    """The PNG at a server-minted workspace-relative path as an inline image, else None."""
    if not workspace_relative_path:
        return None
    return inline_image(
        doc_id=doc_id,
        workspace_relative_path=workspace_relative_path,
        fmt="png",
        inline=True,
        max_output_bytes=None,
    )


def require_doc(doc_id: str) -> None:
    """Fail fast with the standard message when `doc_id` is not an open document."""
    from inkscape_mcp.registry import get_registry

    try:
        get_registry().get(doc_id)
    except KeyError as exc:
        raise ToolError("document id not found") from exc


def doc_summary(doc_id: str) -> dict[str, Any]:
    """The short document summary shared by `open` and `history(reload)`."""
    from inkscape_mcp.document.inspect import inspect_summary

    s = inspect_summary(doc_id)
    return {
        "width": s.width,
        "height": s.height,
        "units": s.units,
        "viewbox": s.viewbox,
        "objects": s.num_objects,
        "layers": s.num_layers,
    }
