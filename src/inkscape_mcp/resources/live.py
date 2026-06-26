"""Live session resources.

Read-only MCP resources exposing live-session state and the current selection. They report
cleanly when no live session is connected (a not-connected `LiveSession`, an empty selection)
rather than raising, so a host can always poll them safely whether or not live is up.
"""

from __future__ import annotations

from inkscape_mcp.live.events import detect_change
from inkscape_mcp.live.records import list_live_operations
from inkscape_mcp.live.scene import get_live_scene
from inkscape_mcp.live.session import get_session_manager
from inkscape_mcp.live.transport import LiveChange, LiveError, LiveScene, LiveSelection
from inkscape_mcp.logging_setup import get_logger
from inkscape_mcp.server import mcp

_logger = get_logger("resources.live")


@mcp.resource("inkscape://live/session", mime_type="application/json")
def live_session() -> str:
    """Current live-session state (enabled, connected, transport, available transports).

    Returns a not-connected `LiveSession` when no session is up — never an error.

    Risk class: low (read-only).
    """
    return get_session_manager().status().model_dump_json()


@mcp.resource("inkscape://live/selection", mime_type="application/json")
def live_selection() -> str:
    """Current selection in the live instance (object ids).

    Returns an empty selection when no live session is connected (clean, not an error path).

    Risk class: low (read-only).
    """
    manager = get_session_manager()
    try:
        transport = manager.require_transport()
        return transport.get_selection().model_dump_json()
    except LiveError:
        return LiveSelection(object_ids=[], count=0).model_dump_json()


@mcp.resource("inkscape://live/view", mime_type="application/json")
def live_view() -> str:
    """Current live frame's structured metadata: the `LiveScene`, without the PNG bytes.

    Exposes the machine-readable scene — active-doc ref, selection ids + bboxes, viewport
    (zoom/center/visible region), canvas size, and a compact visible-object summary (reusing the
    headless object-inspection shape) — so a host can poll the canvas STRUCTURE, not pixels. Pulled
    over the fixed `get_scene` transport command (no code path — ADR-003). Returns an empty
    `LiveScene` when no live session is connected or the transport cannot report a scene (clean, not
    an error path). READ-ONLY — no document mutation, no Operation Record.

    Risk class: low (read-only perception).
    """
    try:
        return get_live_scene().model_dump_json()
    except LiveError:
        return LiveScene().model_dump_json()


@mcp.resource("inkscape://live/events", mime_type="application/json")
def live_events() -> str:
    """Latest live change state: the current cheap state token + classified deltas.

    Polls the cheap state token once (a small revision marker + selection ids + coarse viewport —
    never the full document or a PNG; protocol v5), hashes + diffs it against the session's last
    seen token, and returns the classified `LiveChange` (`selection_changed` / `document_changed` /
    `viewport_changed`, plus the new token). Surfaces the change stream so a host can poll the
    canvas for deltas cheaply without busy-rendering. Returns an empty `LiveChange` when no live
    session is connected or the transport cannot report a token (clean, not an error path).
    READ-ONLY — no document mutation, no Operation Record.

    Risk class: low (read-only perception).
    """
    try:
        return detect_change().model_dump_json()
    except LiveError:
        return LiveChange().model_dump_json()


@mcp.resource("inkscape://live/operations", mime_type="application/json")
def live_operations() -> str:
    """Recent Live Operation Records: what each live mutation changed and how.

    Makes live mutations observable — each record carries the transport, selection, affected ids,
    approval decision, before/after render paths, and status. Returns an empty log when there are
    none or no workspace root is configured (never an error path).

    Risk class: low (read-only).
    """
    return list_live_operations().model_dump_json()
