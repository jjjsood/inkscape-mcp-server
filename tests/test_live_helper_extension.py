"""Helper extension (runs inside Inkscape) tests, driven with an `inkex` stub.

Covers the parts that need no real `inkex`: loopback-only bind, the token handshake, per-message
token checks and unknown-command handling in `_serve`/`_dispatch`. The document-reading commands
(selection, inspect, render, scene, writes) need a real `inkex` document and are NOT exercised here.
"""

from __future__ import annotations

import json
import socket
import threading
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from inkscape_mcp.live.socket_backend import ENV_RENDEZVOUS

from .test_live_socket_backend import _load_helper

TOKEN = "helper-token"


@pytest.fixture
def helper(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    return _load_helper(monkeypatch)


@contextmanager
def _serving(helper: types.ModuleType) -> Iterator[tuple[socket.socket, threading.Thread]]:
    """Run `InkscapeMcpLive._serve` on one end of a socketpair; yield (client end, serve thread).

    `_serve` returning ends the session (`effect()` then closes the connection)."""
    server_end, client_end = socket.socketpair()
    client_end.settimeout(5)
    thread = threading.Thread(
        target=helper.InkscapeMcpLive()._serve, args=(server_end, TOKEN), daemon=True
    )
    thread.start()
    try:
        yield client_end, thread
    finally:
        client_end.close()
        thread.join(5)
        server_end.close()
    assert not thread.is_alive()


def _ask(sock: socket.socket, msg: dict[str, Any]) -> dict[str, Any]:
    sock.sendall(json.dumps(msg).encode() + b"\n")
    line = sock.makefile("rb").readline()
    return json.loads(line)


def _session_ended(thread: threading.Thread) -> bool:
    thread.join(5)
    return not thread.is_alive()


def test_effect_binds_loopback_only(
    helper: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound: list[Any] = []

    class RecordingSocket(socket.socket):
        def bind(self, address: Any) -> None:
            bound.append(address)
            super().bind(address)

    fake_socket_mod = types.SimpleNamespace(
        **{k: getattr(socket, k) for k in ("AF_INET", "SOCK_STREAM", "SOL_SOCKET", "SO_REUSEADDR")},
        socket=RecordingSocket,
    )
    monkeypatch.setattr(helper, "socket", fake_socket_mod)
    monkeypatch.setattr(helper, "IDLE_TIMEOUT_S", 0.05)  # nobody connects -> accept times out
    monkeypatch.setenv(ENV_RENDEZVOUS, str(tmp_path / "rv.json"))

    helper.InkscapeMcpLive().effect()

    assert bound == [("127.0.0.1", 0)]
    assert not (tmp_path / "rv.json").exists()  # rendezvous cleared after the idle timeout


@pytest.mark.parametrize(
    "hello",
    [
        {"cmd": "hello", "token": "WRONG"},
        {"cmd": "hello"},
        {"cmd": "ping", "token": TOKEN},  # first message must be hello
    ],
    ids=["wrong-token", "missing-token", "not-hello"],
)
def test_handshake_rejects_and_ends_session(
    helper: types.ModuleType, hello: dict[str, Any]
) -> None:
    with _serving(helper) as (sock, thread):
        reply = _ask(sock, hello)
        assert reply["ok"] is False
        assert reply["error"] == "unauthorized"
        assert _session_ended(thread)


def test_later_message_with_wrong_token_is_rejected_and_closes(helper: types.ModuleType) -> None:
    with _serving(helper) as (sock, thread):
        hello = _ask(sock, {"cmd": "hello", "token": TOKEN})
        assert hello["ok"] is True
        assert hello["result"]["protocol_version"] == helper.PROTOCOL_VERSION

        reply = _ask(sock, {"cmd": "ping", "token": "WRONG"})
        assert reply == {"v": helper.PROTOCOL_VERSION, "ok": False, "error": "unauthorized"}
        assert _session_ended(thread)


def test_unknown_command_is_an_error_not_a_crash(helper: types.ModuleType) -> None:
    with _serving(helper) as (sock, _thread):
        _ask(sock, {"cmd": "hello", "token": TOKEN})

        for bogus in ("eval", "run_action", ""):
            reply = _ask(sock, {"cmd": bogus, "token": TOKEN})
            assert reply["ok"] is False
            assert reply["error"] == "unknown command"

        # The session survives: a valid command still answers.
        pong = _ask(sock, {"cmd": "ping", "token": TOKEN})
        assert pong["ok"] is True and pong["result"] == {"pong": True}


def test_effect_closes_socket_when_rendezvous_write_fails(
    helper: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

    class RecordingSocket(socket.socket):
        def close(self) -> None:
            closed.append(True)
            super().close()

    fake_socket_mod = types.SimpleNamespace(
        **{k: getattr(socket, k) for k in ("AF_INET", "SOCK_STREAM", "SOL_SOCKET", "SO_REUSEADDR")},
        socket=RecordingSocket,
    )
    monkeypatch.setattr(helper, "socket", fake_socket_mod)

    def planted(*_a: object) -> None:
        raise PermissionError("planted by another user")

    monkeypatch.setattr(helper.InkscapeMcpLive, "_write_rendezvous", planted)
    helper.InkscapeMcpLive().effect()  # returns cleanly, no exception
    assert closed


def test_clear_rendezvous_keeps_a_newer_sessions_file(
    helper: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rv = tmp_path / "rv.json"
    monkeypatch.setenv(ENV_RENDEZVOUS, str(rv))
    rv.write_text(json.dumps({"token": "newer"}), encoding="utf-8")
    helper.InkscapeMcpLive()._clear_rendezvous("mine")
    assert rv.exists()
    helper.InkscapeMcpLive()._clear_rendezvous("newer")
    assert not rv.exists()


def test_bad_token_connection_does_not_end_the_session(
    helper: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rv = tmp_path / "rv.json"
    monkeypatch.setenv(ENV_RENDEZVOUS, str(rv))
    monkeypatch.setattr(helper, "IDLE_TIMEOUT_S", 10.0)
    thread = threading.Thread(target=helper.InkscapeMcpLive().effect, daemon=True)
    thread.start()
    for _ in range(100):
        if rv.exists():
            break
        threading.Event().wait(0.05)
    info = json.loads(rv.read_text(encoding="utf-8"))

    with socket.create_connection(("127.0.0.1", info["port"]), timeout=5) as intruder:
        assert _ask(intruder, {"cmd": "hello", "token": "WRONG"})["error"] == "unauthorized"
    assert thread.is_alive()  # still accepting

    with socket.create_connection(("127.0.0.1", info["port"]), timeout=5) as client:
        assert _ask(client, {"cmd": "hello", "token": info["token"]})["ok"] is True
        assert _ask(client, {"cmd": "ping", "token": info["token"]})["result"] == {"pong": True}
    assert _session_ended(thread)
    assert not rv.exists()
