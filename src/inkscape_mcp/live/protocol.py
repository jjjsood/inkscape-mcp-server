"""Fixed wire schema for the extension-socket bridge.

The schema is the security boundary for the socket transport: it is a FIXED, VERSIONED set of
semantic commands (`LiveCommand`) — there is no command that carries arbitrary code or a raw
Inkscape Action string (ADR-003). Both ends (the server-side socket client in
``socket_backend.py`` and the shipped helper extension in ``helper_extension/``) speak exactly
this schema; the helper cannot import this module (it runs inside Inkscape's interpreter), so it
mirrors these constants and MUST be kept in lock-step — bump ``PROTOCOL_VERSION`` on any change.

Framing: newline-delimited JSON, one object per line, UTF-8. Binary payloads (PNG / SVG) are
base64 in a string field. Reads are hard-capped at ``MAX_MESSAGE_BYTES`` so a hostile or buggy
peer can never exhaust memory. The handshake (``hello`` + token) must succeed before any other
command is accepted.
"""

from __future__ import annotations

import json
import socket
from enum import StrEnum
from typing import Any

#: Wire-schema version. Bump on ANY change to the command set or message shape; the client
#: refuses to talk to a helper advertising a different major version. v2 adds the semantic
#: WRITE commands (apply-to-selection / insert-svg / set-selected-text / export-selection). v3
#: adds the view-only commands (set-viewport) and extends render_view with an optional
#: region/scale. v4 adds the structured-perception command (get_scene: viewport + canvas +
#: selection bboxes + a visible-object summary). v5 adds the CHEAP change-detection command
#: (get_state_token: a small revision marker + selection ids + viewport — never the full doc/PNG —
#: that the server hashes to detect deltas without busy-rendering) — all fixed semantic verbs, still
#: no code/raw-Action member (ADR-003).
PROTOCOL_VERSION = 5

#: Hard cap on a single framed message (64 MiB). Bounds memory against a hostile/runaway peer;
#: large get_document_svg / render_view payloads are base64 and still fit comfortably under it.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024

#: Loopback host the helper binds and the client dials. NEVER a routable address (sec.12).
LOOPBACK_HOST = "127.0.0.1"


class LiveCommand(StrEnum):
    """The complete, fixed semantic command surface. No member permits code/Action passthrough.

    The WRITE members carry only typed, server-validated semantic parameters — a style
    property map, a composed SVG ``transform`` string, an SVG fragment, or a text string — never
    a raw Inkscape Action string or executable code (ADR-003). The VIEW members carry only
    typed, bounded numerics (a viewport mode + zoom/center/delta, or a render region/scale) and the
    READ-only ``get_scene`` / ``get_state_token`` which return typed structured perception
    (no parameters, no code). ``get_state_token`` is the CHEAP change-detection marker: a small
    revision/selection/viewport dict the server hashes — never the full document or a PNG.
    """

    HELLO = "hello"  # handshake: presents the token, returns protocol/version/capabilities
    PING = "ping"  # liveness probe
    GET_ACTIVE_DOCUMENT = "get_active_document"  # active document identity
    GET_SELECTION = "get_selection"  # current selection object ids
    INSPECT_SELECTION = "inspect_selection"  # per-object detail for the selection
    GET_DOCUMENT_SVG = "get_document_svg"  # serialized SVG of the active document (for sync)
    RENDER_VIEW = "render_view"  # rasterized canvas (base64 PNG); optional region/scale
    # --- semantic WRITE surface (mutating; each is approval-gated server-side) ---
    APPLY_TO_SELECTION = "apply_to_selection"  # set validated style + transform on the selection
    INSERT_SVG = "insert_svg"  # insert a safe-parsed SVG fragment into the document
    SET_SELECTED_TEXT = "set_selected_text"  # replace the selected text object's content
    EXPORT_SELECTION = "export_selection"  # rasterize just the current selection (base64 PNG)
    # --- view-only surface (non-mutating; low risk, no Operation Record) ---
    SET_VIEWPORT = "set_viewport"  # zoom / pan / fit-to-selection / fit-to-page (no doc mutation)
    GET_SCENE = "get_scene"  # structured perception: viewport+canvas+selection bboxes+objects
    GET_STATE_TOKEN = "get_state_token"  # noqa: S105 - command id, not a secret; cheap change marker


class ProtocolError(Exception):
    """A message was malformed, over-size, out-of-schema, or the peer hung up mid-frame.

    Public message is stable and carries no host path.
    """


def encode_message(obj: dict[str, Any]) -> bytes:
    """Serialize one schema object to a single newline-terminated UTF-8 JSON frame."""
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    data = line.encode("utf-8") + b"\n"
    if len(data) > MAX_MESSAGE_BYTES:
        raise ProtocolError("outbound message exceeds size cap")
    return data


def build_request(command: LiveCommand, token: str, params: dict[str, Any] | None = None) -> bytes:
    """Build a request frame for a fixed `LiveCommand` (token carried on every request)."""
    return encode_message(
        {
            "v": PROTOCOL_VERSION,
            "cmd": command.value,
            "token": token,
            "params": params or {},
        }
    )


def build_ok(result: dict[str, Any]) -> bytes:
    """Build a success response frame (helper side / test doubles)."""
    return encode_message({"v": PROTOCOL_VERSION, "ok": True, "result": result})


def build_error(message: str) -> bytes:
    """Build an error response frame (helper side / test doubles)."""
    return encode_message({"v": PROTOCOL_VERSION, "ok": False, "error": message})


def recv_message(sock: socket.socket, max_bytes: int = MAX_MESSAGE_BYTES) -> dict[str, Any]:
    """Read one newline-delimited JSON frame from `sock`, enforcing the size cap.

    Raises `ProtocolError` on EOF before a newline, on exceeding `max_bytes`, or on JSON that is
    not a single object. The returned dict is the raw frame; schema validation is the caller's.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = sock.recv(65536)
        except OSError as exc:
            raise ProtocolError(f"socket read failed: {exc}") from exc
        if not chunk:
            raise ProtocolError("connection closed before a complete message")
        newline = chunk.find(b"\n")
        if newline != -1:
            chunks.append(chunk[: newline + 1])
            total += newline + 1
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ProtocolError("inbound message exceeds size cap")
    if total > max_bytes:
        raise ProtocolError("inbound message exceeds size cap")
    raw = b"".join(chunks).rstrip(b"\n")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("message is not valid UTF-8 JSON") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("message is not a JSON object")
    return obj


def parse_response(frame: dict[str, Any]) -> dict[str, Any]:
    """Validate a response frame against the fixed schema and return its `result` dict.

    Enforces the protocol version, the boolean `ok` discriminator, and the result/error shape.
    A non-conforming frame (wrong version, missing fields, error response) raises `ProtocolError`
    so an out-of-schema message can never be mistaken for a valid result.
    """
    if frame.get("v") != PROTOCOL_VERSION:
        raise ProtocolError("protocol version mismatch")
    ok = frame.get("ok")
    if ok is True:
        result = frame.get("result")
        if not isinstance(result, dict):
            raise ProtocolError("malformed success response")
        return result
    if ok is False:
        # Helper-reported error: surface a stable message, never raw internals.
        raise ProtocolError("live helper rejected the request")
    raise ProtocolError("response missing ok discriminator")
