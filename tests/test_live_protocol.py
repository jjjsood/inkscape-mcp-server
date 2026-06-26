"""Fixed-schema wire protocol tests: framing, size caps, handshake, schema validation."""

from __future__ import annotations

import json
import socket

import pytest

from inkscape_mcp.live.protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    LiveCommand,
    ProtocolError,
    build_error,
    build_ok,
    build_request,
    encode_message,
    parse_response,
    recv_message,
)


def test_command_surface_is_fixed_and_has_no_code_passthrough() -> None:
    # The entire command surface is a fixed enum — no "eval"/"action"/"exec"/"run" member.
    # v2 adds the semantic WRITE commands; v3 adds the view-only command; v4 adds the
    # structured-perception read command; v5 adds the cheap change-detection read command. Each
    # carries only typed params, never code.
    names = {c.value for c in LiveCommand}
    assert names == {
        "hello",
        "ping",
        "get_active_document",
        "get_selection",
        "inspect_selection",
        "get_document_svg",
        "render_view",
        "apply_to_selection",
        "insert_svg",
        "set_selected_text",
        "export_selection",
        "set_viewport",
        "get_scene",
        "get_state_token",
    }
    for hostile in ("eval", "exec", "action", "run_action", "script"):
        assert hostile not in names


def test_protocol_version_is_five() -> None:
    # bumped the wire schema to v5 (cheap change-detection read); client refuses a mismatch.
    assert PROTOCOL_VERSION == 5


def test_helper_extension_mirrors_protocol_version_in_lockstep() -> None:
    # The shipped helper cannot import protocol.py; its mirrored constant MUST match (lock-step).
    import ast
    from pathlib import Path

    import inkscape_mcp.live.socket_backend as sb

    helper = Path(sb.__file__).parent / "helper_extension" / "inkscape_mcp_live.py"
    tree = ast.parse(helper.read_text(encoding="utf-8"))
    mirrored: int | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "PROTOCOL_VERSION"
                    and isinstance(node.value, ast.Constant)
                ):
                    mirrored = node.value.value
    assert mirrored == PROTOCOL_VERSION

    # The helper must also mirror the view + perception + change commands + advertise them.
    helper_text = helper.read_text(encoding="utf-8")
    assert 'CMD_SET_VIEWPORT = "set_viewport"' in helper_text
    assert 'CMD_GET_SCENE = "get_scene"' in helper_text
    assert 'CMD_GET_STATE_TOKEN = "get_state_token"' in helper_text


def test_build_request_carries_version_command_and_token() -> None:
    frame = build_request(LiveCommand.PING, "tok", {"x": 1})
    obj = json.loads(frame.decode("utf-8"))
    assert obj == {"v": PROTOCOL_VERSION, "cmd": "ping", "token": "tok", "params": {"x": 1}}
    assert frame.endswith(b"\n")


def test_encode_message_rejects_oversize() -> None:
    huge = {"v": PROTOCOL_VERSION, "blob": "x" * (MAX_MESSAGE_BYTES + 10)}
    with pytest.raises(ProtocolError):
        encode_message(huge)


def test_recv_message_roundtrip_over_socketpair() -> None:
    a, b = socket.socketpair()
    try:
        a.sendall(build_ok({"pong": True}))
        frame = recv_message(b)
        assert parse_response(frame) == {"pong": True}
    finally:
        a.close()
        b.close()


def test_recv_message_raises_on_eof() -> None:
    a, b = socket.socketpair()
    try:
        a.close()  # peer hangs up with no data
        with pytest.raises(ProtocolError):
            recv_message(b)
    finally:
        b.close()


def test_recv_message_enforces_cap() -> None:
    a, b = socket.socketpair()
    try:
        # Stream more than the cap with no newline → must raise, not buffer unbounded.
        a.sendall(b"x" * 2048)
        with pytest.raises(ProtocolError):
            recv_message(b, max_bytes=1024)
    finally:
        a.close()
        b.close()


def test_parse_response_rejects_version_mismatch() -> None:
    with pytest.raises(ProtocolError):
        parse_response({"v": PROTOCOL_VERSION + 1, "ok": True, "result": {}})


def test_parse_response_rejects_error_frame() -> None:
    frame = json.loads(build_error("nope").decode("utf-8"))
    with pytest.raises(ProtocolError):
        parse_response(frame)


def test_parse_response_requires_ok_discriminator() -> None:
    with pytest.raises(ProtocolError):
        parse_response({"v": PROTOCOL_VERSION, "result": {}})
