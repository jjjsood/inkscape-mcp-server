"""Backend probe/rank/selection tests: full per-host set, capability-aware ranking."""

from __future__ import annotations

import pytest

from inkscape_mcp.config import get_settings
from inkscape_mcp.live.dbus_backend import DBusTransport
from inkscape_mcp.live.protocol import LiveCommand
from inkscape_mcp.live.selection import (
    READ_REQUIRED,
    best_available,
    probe_transports,
    select_transport,
)
from inkscape_mcp.live.socket_backend import ExtensionSocketTransport
from inkscape_mcp.live.transport import TransportProbe


def _socket_probe(available: bool) -> object:
    def probe(settings: object) -> TransportProbe:
        return TransportProbe(
            name=ExtensionSocketTransport.name,
            available=available,
            rank=ExtensionSocketTransport.rank,
            supported_commands=[c.value for c in ExtensionSocketTransport.supported_commands],
            detail="patched",
        )

    return probe


def _dbus_probe(available: bool) -> object:
    def probe(settings: object) -> TransportProbe:
        return TransportProbe(
            name=DBusTransport.name,
            available=available,
            rank=DBusTransport.rank,
            supported_commands=[LiveCommand.PING.value],
            detail="patched",
        )

    return probe


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()


def test_probe_returns_full_set_not_one_per_os(
    patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ExtensionSocketTransport, "probe", _socket_probe(False))
    monkeypatch.setattr(DBusTransport, "probe", _dbus_probe(False))
    probes = probe_transports()
    names = {p.name for p in probes}
    # Both transports are always reported, regardless of OS / availability.
    assert names == {"extension-socket", "dbus"}


def test_extension_socket_preferred_for_reads_over_dbus(
    patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ExtensionSocketTransport, "probe", _socket_probe(True))
    monkeypatch.setattr(DBusTransport, "probe", _dbus_probe(True))
    best = best_available(required=READ_REQUIRED)
    assert best is not None and best.name == "extension-socket"
    chosen = select_transport(required=READ_REQUIRED)
    assert isinstance(chosen, ExtensionSocketTransport)


def test_dbus_alone_cannot_serve_reads(patched: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # Only DBus available → it lacks the read commands, so no transport is selectable for reads.
    monkeypatch.setattr(ExtensionSocketTransport, "probe", _socket_probe(False))
    monkeypatch.setattr(DBusTransport, "probe", _dbus_probe(True))
    assert best_available(required=READ_REQUIRED) is None
    assert select_transport(required=READ_REQUIRED) is None
    # But it IS the best available when only liveness is required.
    liveness = best_available(required=frozenset())
    assert liveness is not None and liveness.name == "dbus"


def test_no_transport_available_returns_none(
    patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ExtensionSocketTransport, "probe", _socket_probe(False))
    monkeypatch.setattr(DBusTransport, "probe", _dbus_probe(False))
    assert best_available(required=frozenset()) is None
    assert select_transport() is None


def test_a_failing_backend_probe_never_breaks_detection(
    patched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(settings: object) -> TransportProbe:
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(ExtensionSocketTransport, "probe", boom)
    monkeypatch.setattr(DBusTransport, "probe", _dbus_probe(False))
    probes = probe_transports()
    socket_probe = next(p for p in probes if p.name == "extension-socket")
    assert socket_probe.available is False
    assert "probe failed" in socket_probe.detail
