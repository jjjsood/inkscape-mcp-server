"""Socket client robustness: loopback-only dialing, timeouts, hang-ups, hostile/garbled frames.

Every failure mode must surface as `LiveConnectionError` (a `LiveError` the session/tool layer
handles) or, at the raw framing layer, `ProtocolError` — never another exception type.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, Settings, get_settings
from inkscape_mcp.live import socket_backend
from inkscape_mcp.live.protocol import (
    LOOPBACK_HOST,
    PROTOCOL_VERSION,
    ProtocolError,
    build_ok,
    encode_message,
    parse_response,
    recv_message,
)
from inkscape_mcp.live.socket_backend import ENV_RENDEZVOUS, ExtensionSocketTransport
from inkscape_mcp.live.transport import LiveConnectionError

from .conftest import mock_helper

TOKEN = "secret-token"
HELLO_OK = build_ok({"protocol_version": PROTOCOL_VERSION, "capabilities": []})


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    # Tiny socket timeout so the "never answers" cases stay fast.
    return get_settings().model_copy(update={"process_timeout_s": 0.2})


def _advertise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: int, **extra: object) -> None:
    rv = tmp_path / "rv.json"
    payload = {"port": port, "token": TOKEN, "protocol_version": PROTOCOL_VERSION, **extra}
    rv.write_text(json.dumps(payload), encoding="utf-8")
    rv.chmod(0o600)
    monkeypatch.setenv(ENV_RENDEZVOUS, str(rv))


@contextmanager
def _scripted_helper(script: Callable[[socket.socket], None]) -> Iterator[int]:
    """One-connection loopback peer that runs `script(conn)`; yields its port."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind((LOOPBACK_HOST, 0))
    srv.listen(1)

    def run() -> None:
        with suppress(OSError):
            conn, _ = srv.accept()
            with conn, suppress(OSError, ProtocolError):
                script(conn)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield srv.getsockname()[1]
    finally:
        srv.close()
        thread.join(5)


def _wait_for_hangup(conn: socket.socket) -> None:
    conn.settimeout(5)
    conn.recv(1)  # blocks until the client gives up and closes


def _handshake(conn: socket.socket) -> None:
    recv_message(conn)
    conn.sendall(HELLO_OK)


# --- gap 3: loopback-only dialing ------------------------------------------


def test_client_dials_loopback_and_ignores_rendezvous_host(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialed: list[tuple[str, int]] = []

    def fake_create_connection(address: tuple[str, int], timeout: float) -> socket.socket:
        dialed.append(address)
        raise OSError("refused")

    monkeypatch.setattr(socket_backend.socket, "create_connection", fake_create_connection)
    _advertise(tmp_path, monkeypatch, 4321, host="10.0.0.5")

    with pytest.raises(LiveConnectionError):
        ExtensionSocketTransport(settings).connect()
    assert dialed == [(LOOPBACK_HOST, 4321)]


# --- gap 4: silent peer / mid-session hang-up -------------------------------


def test_helper_never_answers_handshake_times_out(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _scripted_helper(_wait_for_hangup) as port:
        _advertise(tmp_path, monkeypatch, port)
        transport = ExtensionSocketTransport(settings)
        start = time.monotonic()
        with pytest.raises(LiveConnectionError):
            transport.connect()
        assert time.monotonic() - start < 3
        assert not transport.is_connected()


def test_helper_stops_answering_mid_session_times_out(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def script(conn: socket.socket) -> None:
        _handshake(conn)
        _wait_for_hangup(conn)

    with _scripted_helper(script) as port:
        _advertise(tmp_path, monkeypatch, port)
        transport = ExtensionSocketTransport(settings)
        transport.connect()
        with pytest.raises(LiveConnectionError):
            transport.get_selection()
        transport.disconnect()


def test_peer_closes_mid_session_is_clean_error(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def script(conn: socket.socket) -> None:
        _handshake(conn)
        recv_message(conn)  # read the request, then hang up without replying

    with _scripted_helper(script) as port:
        _advertise(tmp_path, monkeypatch, port)
        transport = ExtensionSocketTransport(settings)
        transport.connect()
        with pytest.raises(LiveConnectionError):
            transport.get_document_svg()
        transport.disconnect()


# --- gap 5: protocol-version mismatch in the handshake ----------------------


@pytest.mark.parametrize(
    "hello_reply",
    [
        build_ok({"protocol_version": PROTOCOL_VERSION + 1, "capabilities": []}),
        build_ok({"capabilities": []}),
        encode_message({"v": PROTOCOL_VERSION + 1, "ok": True, "result": {"protocol_version": 99}}),
    ],
    ids=["result-version", "no-version", "frame-version"],
)
def test_handshake_with_other_protocol_version_is_refused(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hello_reply: bytes
) -> None:
    def script(conn: socket.socket) -> None:
        recv_message(conn)
        conn.sendall(hello_reply)
        _wait_for_hangup(conn)

    with _scripted_helper(script) as port:
        _advertise(tmp_path, monkeypatch, port)
        transport = ExtensionSocketTransport(settings)
        with pytest.raises(LiveConnectionError):
            transport.connect()
        assert not transport.is_connected()


# --- gap 6: garbled frames ---------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [b"not json\n", b"[1, 2, 3]\n", b'"just a string"\n', b"\xff\xfe\xfd\n"],
    ids=["non-json", "json-array", "json-string", "non-utf8"],
)
def test_recv_message_rejects_garbage(raw: bytes) -> None:
    a, b = socket.socketpair()
    try:
        a.sendall(raw)
        with pytest.raises(ProtocolError):
            recv_message(b)
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("result", [None, [], "x", 1])
def test_parse_response_rejects_non_dict_result(result: object) -> None:
    with pytest.raises(ProtocolError):
        parse_response({"v": PROTOCOL_VERSION, "ok": True, "result": result})


@pytest.mark.parametrize(
    "reply",
    [
        b"not json\n",
        b"\xff\xfe\n",
        encode_message({"v": PROTOCOL_VERSION, "ok": True, "result": ["a"]}),
        encode_message({"v": PROTOCOL_VERSION, "ok": False, "error": "command failed"}),
    ],
    ids=["non-json", "non-utf8", "result-not-dict", "helper-error"],
)
def test_garbled_or_rejected_reply_is_live_connection_error(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: bytes
) -> None:
    def script(conn: socket.socket) -> None:
        _handshake(conn)
        recv_message(conn)
        conn.sendall(reply)
        _wait_for_hangup(conn)

    with _scripted_helper(script) as port:
        _advertise(tmp_path, monkeypatch, port)
        transport = ExtensionSocketTransport(settings)
        transport.connect()
        with pytest.raises(LiveConnectionError):
            transport.get_selection()
        transport.disconnect()


def test_request_caps_reply_at_max_output_bytes(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capped = settings.model_copy(update={"max_output_bytes": 1024, "process_timeout_s": 5.0})
    with mock_helper(token=TOKEN, handler=lambda cmd, params: {"svg": "x" * 4096}) as port:
        _advertise(tmp_path, monkeypatch, port)
        transport = ExtensionSocketTransport(capped)
        transport.connect()  # the small hello reply fits under the cap
        with pytest.raises(LiveConnectionError):
            transport.get_document_svg()
        transport.disconnect()
