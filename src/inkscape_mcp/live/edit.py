"""Live semantic edit engine (E4-01, ADR-003/004/005).

The kernel behind the live WRITE tools. It reuses the E2 safe-edit *semantics* (the same colour /
length / number / text validators) to build typed, injection-safe parameters, then routes the
change through the connected `LiveTransport` so the same tool works on whichever backend the host
has. There is NO arbitrary code path and NO raw Action string — a mutation is only ever a
validated style-property map, a composed SVG ``transform``, a safe-parsed SVG fragment, or a text
string (ADR-003).

Governance (E4-02) is woven in by :func:`run_live_mutation`: it gates the op behind the risk /
approval policy BEFORE touching the instance, opens a Live Operation Record, captures a
before/after canvas render, and marks the record applied/discarded — so every live mutation is
observable, undo-friendly where the backend allows, and syncable to a workspace snapshot.

SAFETY (sec.12): every client value is validated through a `dom` normalizer or a bounded pattern
check here before it crosses the transport boundary; SVG fragments are parsed through the
normative safe parser (no entities / no network / no DTD). Nothing is interpolated into argv.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable

from pydantic import BaseModel, Field

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.edit.dom import (
    EditError,
    fmt_num,
    normalize_color,
    normalize_length,
)
from inkscape_mcp.live.protocol import LiveCommand
from inkscape_mcp.live.records import (
    LiveOperationRecord,
    new_live_operation,
    update_live_operation,
)
from inkscape_mcp.live.render import render_live_view
from inkscape_mcp.live.session import LiveSessionManager, get_session_manager
from inkscape_mcp.live.transport import (
    LiveCapabilityUnsupported,
    LiveError,
    LiveMutationResult,
    LiveSelection,
    LiveTransport,
    LiveViewportResult,
    RenderRegion,
)
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.operations import OperationStatus
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.limits import check_output_size
from inkscape_mcp.workspace.risk import RiskClass
from inkscape_mcp.workspace.xml_safety import UnsafeXMLError, parse_svg_bytes

_logger = get_logger("live.edit")

#: Max SVG-fragment size accepted by `live_insert_svg` (bytes). Bounds a single insert well below
#: the document input cap.
_MAX_FRAGMENT_BYTES = 1 * 1024 * 1024

#: Max text length for `live_set_selected_text` (mirrors the E2 `replace_text` bound).
_MAX_TEXT_LEN = 100_000

#: Control chars forbidden in live text content (C0/C1 except tab / newline / carriage return).
_FORBIDDEN_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

#: Wrapper used only to safe-parse a (possibly multi-root) SVG fragment for validation.
_FRAGMENT_WRAP = (
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">{}</svg>'
)


class LiveEditResult(BaseModel):
    """Outcome of one live mutation: the Live Operation Record id + what changed + renders.

    `preview_before` / `preview_after` are WORKSPACE-RELATIVE PNG paths of the canvas captured
    around the mutation (or `None` when a render was unavailable — best-effort, never blocks).
    """

    operation_id: str
    transport: str | None = None
    summary: str = ""
    affected_ids: list[str] = Field(default_factory=list)
    count: int = 0
    undo_friendly: bool = False
    preview_before: str | None = None
    preview_after: str | None = None


class LiveExportResult(BaseModel):
    """Outcome of `live_export_selection`: a workspace-relative PNG path + its size + ids."""

    artifact_path: str
    format: str
    size_bytes: int
    object_ids: list[str] = Field(default_factory=list)


# --- Validated parameter builders (reuse E2 semantics) ----------------------


def _check_opacity(value: float) -> str:
    """Validate an opacity in [0, 1] and format it compactly (reuses the E2 bound)."""
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise EditError("opacity must be a number between 0 and 1") from exc
    if not math.isfinite(num) or not (0.0 <= num <= 1.0):
        raise EditError("opacity must be between 0 and 1")
    text = f"{num:.6f}".rstrip("0").rstrip(".")
    return text if text else "0"


def build_style(
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
    opacity: float | None = None,
) -> dict[str, str]:
    """Build a validated CSS style-property map from typed inputs (reuses the E2 validators).

    Each value is normalized/canonicalized exactly as the headless style tools do, so a value
    carrying CSS-injection punctuation is rejected here, before it crosses the transport boundary.
    Returns only the supplied properties (possibly empty).
    """
    style: dict[str, str] = {}
    if fill is not None:
        style["fill"] = normalize_color(fill)
    if stroke is not None:
        style["stroke"] = normalize_color(stroke)
    if stroke_width is not None:
        style["stroke-width"] = normalize_length(stroke_width)
    if opacity is not None:
        style["opacity"] = _check_opacity(opacity)
    return style


def build_transform(
    dx: float | None = None,
    dy: float | None = None,
    scale: float | None = None,
    rotate: float | None = None,
) -> str | None:
    """Compose a validated SVG `transform` string from simple move/scale/rotate inputs.

    Mirrors the E2 transform engine's safety: every number goes through `fmt_num` (finite-checked),
    `scale` must be finite and positive, and a translate needs BOTH `dx` and `dy`. Order is
    ``translate rotate scale``. Returns `None` when no transform part was requested.
    """
    if (dx is None) != (dy is None):
        raise EditError("translate requires both dx and dy")
    parts: list[str] = []
    if dx is not None and dy is not None:
        parts.append(f"translate({fmt_num(dx)},{fmt_num(dy)})")
    if rotate is not None:
        parts.append(f"rotate({fmt_num(rotate)})")
    if scale is not None:
        if not math.isfinite(scale) or scale <= 0:
            raise EditError("scale factor must be positive")
        parts.append(f"scale({fmt_num(scale)})")
    return " ".join(parts) if parts else None


def validate_svg_fragment(fragment: str) -> str:
    """Validate an SVG fragment through the normative safe parser; return it unchanged.

    The fragment is wrapped in an `<svg>` container (so a multi-element fragment is accepted) and
    parsed with the safe parser, which blocks entities / external DTDs / network fetches. Bounds
    the size and rejects an empty fragment. Raises `EditError` on any failure (stable message).
    """
    if not fragment.strip():
        raise EditError("svg fragment is empty")
    if len(fragment.encode("utf-8")) > _MAX_FRAGMENT_BYTES:
        raise EditError("svg fragment is too large")
    try:
        parse_svg_bytes(_FRAGMENT_WRAP.format(fragment).encode("utf-8"))
    except UnsafeXMLError as exc:
        raise EditError("svg fragment could not be parsed safely") from exc
    return fragment


def validate_text(text: str) -> str:
    """Validate live text content (length bound + forbidden control chars). Reuses E2 guards."""
    if len(text) > _MAX_TEXT_LEN:
        raise EditError(f"text too long: {len(text)} > {_MAX_TEXT_LEN} characters")
    if _FORBIDDEN_CTRL_RE.search(text):
        raise EditError("text contains forbidden control characters")
    return text


# --- View-only parameter validators (E8) ------------------------------------

#: Viewport modes the agent may request — a fixed semantic set (no raw Action/code passthrough).
VIEWPORT_MODES = frozenset({"zoom", "pan", "fit_selection", "fit_page"})

#: Bounds for view numerics — wide enough for any real canvas, tight enough to refuse a runaway.
_MAX_ZOOM = 10_000.0
_MIN_ZOOM = 1e-4
_MAX_COORD = 1e7
_MAX_REGION_EXTENT = 1e7
_MAX_RENDER_SCALE = 64.0
_MIN_RENDER_SCALE = 1e-3

#: Default downscale for the loop's FAST perceive frame (E8-06). A `fast` render with no explicit
#: scale uses this — a half-resolution raster the agent can look at cheaply during rapid iteration;
#: full-res stays available on demand (omit `fast`, or pass an explicit `scale`).
FAST_RENDER_SCALE = 0.5


def _finite_bounded(value: float, limit: float, what: str) -> float:
    """Coerce to a finite float within ``[-limit, limit]`` or raise a stable `EditError`."""
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise EditError(f"{what} must be a number") from exc
    if not math.isfinite(num):
        raise EditError(f"{what} must be finite")
    if abs(num) > limit:
        raise EditError(f"{what} is out of bounds")
    return num


def validate_viewport(
    mode: str,
    zoom: float | None = None,
    center_x: float | None = None,
    center_y: float | None = None,
    dx: float | None = None,
    dy: float | None = None,
) -> dict[str, object]:
    """Validate viewport params per mode; return a typed kwargs dict for `transport.set_viewport`.

    The `mode` must be one of the fixed `VIEWPORT_MODES`. Every numeric is finite-checked and
    bounded HERE, before it crosses the transport boundary (sec.12): `zoom` is positive and bounded
    (`zoom` mode), `center` is a bounded coordinate pair (optional for `zoom`), and `dx`/`dy` are a
    bounded delta pair required together (`pan` mode). `fit_selection`/`fit_page` take no numerics.
    """
    if mode not in VIEWPORT_MODES:
        raise EditError(f"unknown viewport mode: {mode!r}")
    out: dict[str, object] = {"mode": mode}
    if mode == "zoom":
        if zoom is None:
            raise EditError("zoom mode requires a zoom factor")
        num = _finite_bounded(zoom, _MAX_ZOOM, "zoom")
        if not (_MIN_ZOOM <= num <= _MAX_ZOOM):
            raise EditError("zoom factor is out of bounds")
        out["zoom"] = num
        if (center_x is None) != (center_y is None):
            raise EditError("center requires both center_x and center_y")
        if center_x is not None and center_y is not None:
            out["center"] = (
                _finite_bounded(center_x, _MAX_COORD, "center_x"),
                _finite_bounded(center_y, _MAX_COORD, "center_y"),
            )
    elif mode == "pan":
        if dx is None or dy is None:
            raise EditError("pan mode requires both dx and dy")
        out["dx"] = _finite_bounded(dx, _MAX_COORD, "dx")
        out["dy"] = _finite_bounded(dy, _MAX_COORD, "dy")
    # fit_selection / fit_page take no numeric parameters.
    return out


def validate_region(x: float, y: float, width: float, height: float) -> RenderRegion:
    """Validate a render region (E8): finite/bounded origin + positive, bounded extent.

    Raises a stable `EditError` on any non-finite, out-of-bounds, or non-positive value before the
    region crosses the transport. View-only — produces no mutation.
    """
    rx = _finite_bounded(x, _MAX_COORD, "region x")
    ry = _finite_bounded(y, _MAX_COORD, "region y")
    rw = _finite_bounded(width, _MAX_REGION_EXTENT, "region width")
    rh = _finite_bounded(height, _MAX_REGION_EXTENT, "region height")
    if rw <= 0 or rh <= 0:
        raise EditError("region width/height must be positive")
    return RenderRegion(x=rx, y=ry, width=rw, height=rh)


def validate_render_scale(scale: float) -> float:
    """Validate a render scale factor (E8): finite, positive, and within bounds."""
    num = _finite_bounded(scale, _MAX_RENDER_SCALE, "scale")
    if not (_MIN_RENDER_SCALE <= num <= _MAX_RENDER_SCALE):
        raise EditError("scale is out of bounds")
    return num


# --- Orchestrator -----------------------------------------------------------


def _safe_render(manager: LiveSessionManager, settings: Settings) -> str | None:
    """Render the live canvas, returning its rel path or None (best-effort, never raises out)."""
    try:
        return render_live_view(manager=manager, settings=settings).artifact_path
    except LiveError:
        return None
    except Exception:  # pragma: no cover - render is feedback only; never block a mutation
        _logger.warning("live before/after render unavailable", extra={"event": "preview"})
        return None


def _safe_selection(transport: LiveTransport) -> LiveSelection:
    try:
        return transport.get_selection()
    except LiveError:
        return LiveSelection(object_ids=[], count=0)


def run_live_mutation(
    *,
    tool: str,
    params: dict[str, object],
    required_command: LiveCommand,
    op: Callable[[LiveTransport], LiveMutationResult],
    approval_token: str | None,
    manager: LiveSessionManager | None = None,
    settings: Settings | None = None,
) -> LiveEditResult:
    """Run one semantic live mutation as a governed, recorded, reversible step (E4-01 + E4-02).

    Order (fail-safe): require a connected transport → refuse if it cannot serve the command →
    capture document + selection context → open a Live Operation Record (this enforces the risk /
    approval policy, so a HIGH-risk op without `approval_token` raises `PolicyViolation` here,
    before any mutation) → before-render → run `op` → after-render → mark the record applied. A
    `LiveError` from `op` marks the record discarded and re-raises; nothing partial is recorded as
    applied.
    """
    s = settings if settings is not None else get_settings()
    mgr = manager if manager is not None else get_session_manager()
    transport = mgr.require_transport()
    if not transport.supports(required_command):
        raise LiveCapabilityUnsupported("the active live transport cannot perform this edit")

    document = mgr.status().active_document
    selection = _safe_selection(transport)

    # Opening the record enforces the approval gate FIRST — never mutate unapproved (X1).
    record: LiveOperationRecord = new_live_operation(
        tool=tool,
        risk_class=RiskClass.HIGH,
        params=params,
        transport=transport.name,
        document=document,
        selection=selection.object_ids,
        approval_token=approval_token,
        settings=s,
    )

    preview_before = _safe_render(mgr, s)

    try:
        result = op(transport)
    except LiveError:
        update_live_operation(record, settings=s, status=OperationStatus.DISCARDED)
        raise

    preview_after = _safe_render(mgr, s)
    previews = {
        phase: path
        for phase, path in (("before", preview_before), ("after", preview_after))
        if path is not None
    }
    update_live_operation(
        record,
        settings=s,
        status=OperationStatus.APPLIED,
        previews=previews,
        affected_ids=result.affected_ids,
        undo_friendly=result.undo_friendly,
    )
    log_tool_call(
        _logger,
        tool=tool,
        operation_id=record.operation_id,
        transport=transport.name,
        affected=result.count,
    )
    return LiveEditResult(
        operation_id=record.operation_id,
        transport=transport.name,
        summary=result.detail or tool,
        affected_ids=result.affected_ids,
        count=result.count,
        undo_friendly=result.undo_friendly,
        preview_before=preview_before,
        preview_after=preview_after,
    )


def export_live_selection(
    manager: LiveSessionManager | None = None, settings: Settings | None = None
) -> LiveExportResult:
    """Export just the current live selection to a PNG under the live artifacts dir (low risk).

    Read-only feedback: like `live_render_view`, it touches no workspace document and produces no
    Operation Record. Writes atomically (temp + replace), enforces the output-size cap, and returns
    a workspace-relative path.
    """
    from datetime import UTC, datetime

    s = settings if settings is not None else get_settings()
    mgr = manager if manager is not None else get_session_manager()
    transport = mgr.require_transport()
    if not transport.supports(LiveCommand.EXPORT_SELECTION):
        raise LiveCapabilityUnsupported("the active live transport cannot export the selection")

    selection = _safe_selection(transport)
    png = transport.export_selection()

    if not s.workspace_roots:
        raise LiveError("no workspace root configured to store the selection export")
    root = s.workspace_roots[0]
    sandbox.ensure_live_dirs(root)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = sandbox.live_artifacts_dir(root) / f"live-selection-{stamp}.png"

    tmp = out.with_name(f"{out.name}.tmp")
    tmp.write_bytes(png)
    tmp.replace(out)
    try:
        check_output_size(out, s)
    except Exception:
        out.unlink(missing_ok=True)
        raise

    rel = out.relative_to(root).as_posix()
    log_tool_call(_logger, tool="live_export_selection", count=selection.count)
    return LiveExportResult(
        artifact_path=rel,
        format="png",
        size_bytes=out.stat().st_size,
        object_ids=selection.object_ids,
    )


def set_live_viewport(
    kwargs: dict[str, object],
    manager: LiveSessionManager | None = None,
    settings: Settings | None = None,
) -> LiveViewportResult:
    """Drive the live canvas viewport over the connected transport (E8, low risk).

    VIEW-ONLY: like `render_live_view`/`export_live_selection`, it changes only how the canvas is
    displayed — it touches no workspace document and produces NO Operation Record (it never routes
    through `run_live_mutation`). `kwargs` MUST already be validated by `validate_viewport`. Raises
    `LiveCapabilityUnsupported` when the active transport cannot drive the viewport.
    """
    mgr = manager if manager is not None else get_session_manager()
    transport = mgr.require_transport()
    if not transport.supports(LiveCommand.SET_VIEWPORT):
        raise LiveCapabilityUnsupported("the active live transport cannot control the viewport")
    result = transport.set_viewport(**kwargs)  # type: ignore[arg-type]
    log_tool_call(_logger, tool="live_set_viewport", mode=result.mode)
    return result
