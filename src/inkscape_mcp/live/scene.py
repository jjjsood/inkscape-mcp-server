"""Structured live-scene perception (ADR-006, low risk).

The decisive "better than inkmcp" capability: every live frame is paired with a machine-readable
``LiveScene`` so the agent reasons over STRUCTURE, not pixels. The scene is pulled over the fixed
transport schema (``GET_SCENE``, protocol v4) — there is no code/raw-Action path (ADR-003) — and is
fully typed + server-modeled before it reaches a tool.

The ``LiveScene`` models live alongside the other shared read models in ``transport.py``; its
visible-object summary REUSES the headless ``ObjectInfo`` shape (``document/inspect.py``) so
headless and live perception stay aligned — there is no parallel divergent object model.

This module holds the DEFENSIVE coercion of a raw ``get_scene`` wire result into the typed model
(used by the socket backend) and the read-only orchestrator (``get_live_scene``). READ-ONLY:
assembling a scene never mutates the live document, so it produces NO Operation Record and never
routes through ``run_live_mutation`` (it mirrors ``render_live_view`` / ``set_viewport``). All
numerics are coerced/bounded on the way in; messages are size-capped by the transport.
"""

from __future__ import annotations

import math
from typing import Any

from inkscape_mcp.document.inspect import ObjectInfo
from inkscape_mcp.live.session import LiveSessionManager, get_session_manager
from inkscape_mcp.live.transport import (
    BBox,
    LiveDocumentRef,
    LiveScene,
    LiveTransport,
    SceneCanvas,
    SceneSelectionItem,
    SceneViewport,
)
from inkscape_mcp.logging_setup import get_logger

_logger = get_logger("live.scene")

#: Upper bound on selection / visible-object entries coerced from one ``get_scene`` payload. Bounds
#: the response a hostile/runaway helper can produce even within the 64 MiB frame cap; excess
#: entries are dropped with a logged warning (read-only — truncation never loses document data).
_MAX_SCENE_ITEMS = 10_000

# --- Defensive coercion of the wire payload --------------------------------


def _opt_float(value: Any) -> float | None:
    """Coerce a JSON number to a finite float, or None (rejects NaN/inf, bools, non-numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    num = float(value)
    return num if math.isfinite(num) else None


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _bbox(raw: Any) -> BBox | None:
    """Coerce a ``[x, y, w, h]`` list (or ``{x,y,width,height}`` dict) to a `BBox`, else None."""
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        nums = [_opt_float(v) for v in raw]
        if all(n is not None for n in nums):
            return BBox(x=nums[0], y=nums[1], width=nums[2], height=nums[3])  # type: ignore[arg-type]
        return None
    if isinstance(raw, dict):
        x, y = _opt_float(raw.get("x")), _opt_float(raw.get("y"))
        w, h = _opt_float(raw.get("width")), _opt_float(raw.get("height"))
        if None not in (x, y, w, h):
            return BBox(x=x, y=y, width=w, height=h)  # type: ignore[arg-type]
    return None


def _selection(raw: Any) -> list[SceneSelectionItem]:
    items: list[SceneSelectionItem] = []
    if not isinstance(raw, list):
        return items
    if len(raw) > _MAX_SCENE_ITEMS:
        _logger.warning("scene selection truncated", extra={"received": len(raw)})
        raw = raw[:_MAX_SCENE_ITEMS]
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        oid = _opt_str(entry.get("id"))
        if oid is None:
            continue
        items.append(SceneSelectionItem(id=oid, bbox=_bbox(entry.get("bbox"))))
    return items


def _viewport(raw: Any) -> SceneViewport:
    if not isinstance(raw, dict):
        return SceneViewport()
    center_raw = raw.get("center")
    center: tuple[float, float] | None = None
    if isinstance(center_raw, (list, tuple)) and len(center_raw) == 2:
        cx, cy = _opt_float(center_raw[0]), _opt_float(center_raw[1])
        if cx is not None and cy is not None:
            center = (cx, cy)
    return SceneViewport(
        zoom=_opt_float(raw.get("zoom")),
        center=center,
        visible_region=_bbox(raw.get("visible_region")),
    )


def _canvas(raw: Any) -> SceneCanvas:
    if not isinstance(raw, dict):
        return SceneCanvas()
    return SceneCanvas(
        width=_opt_float(raw.get("width")),
        height=_opt_float(raw.get("height")),
        units=_opt_str(raw.get("units")),
    )


def _visible_objects(raw: Any) -> list[ObjectInfo]:
    objects: list[ObjectInfo] = []
    if not isinstance(raw, list):
        return objects
    if len(raw) > _MAX_SCENE_ITEMS:
        _logger.warning("scene visible_objects truncated", extra={"received": len(raw)})
        raw = raw[:_MAX_SCENE_ITEMS]
    for item in raw:
        if not isinstance(item, dict):
            continue
        objects.append(
            ObjectInfo(
                id=_opt_str(item.get("id")),
                tag=str(item.get("tag", "")),
                label=_opt_str(item.get("label")),
                has_style=bool(item.get("has_style", False)),
            )
        )
    return objects


def scene_from_result(result: dict[str, Any], active_document: LiveDocumentRef | None) -> LiveScene:
    """Build a `LiveScene` from a raw ``get_scene`` result dict (defensive coercion).

    Every field is coerced/bounded server-side; an absent or malformed field degrades to an empty /
    null value rather than raising, so a buggy peer can never inject untyped data into the model.
    The active document is taken from the connected session (authoritative) rather than the wire.
    """
    selection = _selection(result.get("selection"))
    visible_objects = _visible_objects(result.get("visible_objects"))
    return LiveScene(
        active_document=active_document,
        selection=selection,
        selection_count=len(selection),
        viewport=_viewport(result.get("viewport")),
        canvas=_canvas(result.get("canvas")),
        visible_objects=visible_objects,
        object_count=len(visible_objects),
    )


def get_live_scene(manager: LiveSessionManager | None = None) -> LiveScene:
    """Pull the structured `LiveScene` from the connected transport (read-only perception).

    Requires an established session (raises `LiveNotAvailable` via ``require_transport`` otherwise)
    and a transport that serves ``get_scene`` (raises `LiveCapabilityUnsupported` otherwise). Never
    mutates the live document and produces no Operation Record.
    """
    mgr = manager if manager is not None else get_session_manager()
    transport: LiveTransport = mgr.require_transport()
    return transport.get_scene()
