"""Live session manager tests: master gate, connect/disconnect, gate default-on/opt-out."""

from __future__ import annotations

from pathlib import Path

import pytest

from inkscape_mcp.config import Settings
from inkscape_mcp.live import session as session_mod
from inkscape_mcp.live.records import list_live_operations, new_live_operation
from inkscape_mcp.live.session import LiveSessionManager
from inkscape_mcp.live.transport import (
    LiveDisabled,
    LiveNotAvailable,
    TransportProbe,
)
from inkscape_mcp.workspace.risk import RiskClass

from .conftest import FakeTransport


def _probes(available: list[str]) -> list[TransportProbe]:
    return [
        TransportProbe(name=n, available=True, rank=1, supported_commands=[], detail="")
        for n in available
    ]


@pytest.fixture(autouse=True)
def _quiet_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep status() off real subprocesses; report a single available transport by default, and keep
    # the R10 capability-probe reconciliation off the real host (tests opt in explicitly).
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: _probes(["fake"]))
    monkeypatch.setattr(session_mod, "_extension_socket_installed", lambda: False)


def _settings(tmp_path: Path, enabled: bool) -> Settings:
    return Settings(workspace_roots=[tmp_path], live_enabled=enabled)


def test_default_off_connect_refused(tmp_path: Path) -> None:
    mgr = LiveSessionManager(_settings(tmp_path, enabled=False))
    with pytest.raises(LiveDisabled):
        mgr.connect()
    status = mgr.status()
    assert status.enabled is False
    assert status.connected is False
    assert any("disabled" in n for n in status.notes)


def test_connect_records_transport_and_active_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_mod, "select_transport", lambda settings, required: FakeTransport())
    mgr = LiveSessionManager(_settings(tmp_path, enabled=True))

    status = mgr.connect()
    assert status.connected is True
    assert status.transport == "fake"
    assert status.active_document is not None
    assert status.active_document.name == "live.svg"
    assert status.connected_at is not None

    # require_transport hands back the live transport for the read tools.
    assert mgr.require_transport().is_connected()


def test_connect_with_no_transport_is_clean_not_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_mod, "select_transport", lambda settings, required: None)
    mgr = LiveSessionManager(_settings(tmp_path, enabled=True))
    with pytest.raises(LiveNotAvailable):
        mgr.connect()
    assert mgr.status().connected is False


def test_disconnect_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_mod, "select_transport", lambda settings, required: FakeTransport())
    mgr = LiveSessionManager(_settings(tmp_path, enabled=True))
    mgr.connect()
    assert mgr.disconnect().connected is False
    # Second disconnect must not raise.
    assert mgr.disconnect().connected is False
    with pytest.raises(LiveNotAvailable):
        mgr.require_transport()


# ---: live op state is scoped + cleared on the session boundary --------------


def test_connect_clears_stale_prior_session_op_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate a record left over from a PRIOR session sitting on disk before this connect.
    s = _settings(tmp_path, enabled=True)
    new_live_operation(
        tool="live_apply_to_selection",
        risk_class=RiskClass.HIGH,
        params={},
        approval_token="ok",
        settings=s,
    )
    assert list_live_operations(settings=s).count == 1

    monkeypatch.setattr(session_mod, "select_transport", lambda settings, required: FakeTransport())
    mgr = LiveSessionManager(s)
    mgr.connect()  # connecting a new session must scrub the stale record

    assert list_live_operations(settings=s).count == 0


def test_disconnect_clears_session_op_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _settings(tmp_path, enabled=True)
    monkeypatch.setattr(session_mod, "select_transport", lambda settings, required: FakeTransport())
    mgr = LiveSessionManager(s)
    mgr.connect()
    # A record produced during the session...
    new_live_operation(
        tool="live_insert_svg",
        risk_class=RiskClass.HIGH,
        params={},
        approval_token="ok",
        settings=s,
    )
    assert list_live_operations(settings=s).count == 1

    mgr.disconnect()  # ...must not survive past the session boundary
    assert list_live_operations(settings=s).count == 0


# --- R10: available_transports reconciliation --------------------------------


def test_available_transports_includes_extension_socket_when_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Probe reports only dbus available (no socket session advertising a rendezvous yet), but the
    # capability probe reports the helper installed -> the session list must include it too (R10).
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: _probes(["dbus"]))
    monkeypatch.setattr(session_mod, "_extension_socket_installed", lambda: True)
    mgr = LiveSessionManager(_settings(tmp_path, enabled=True))

    status = mgr.status()
    assert "dbus" in status.available_transports
    assert "extension-socket" in status.available_transports


def test_available_transports_omits_extension_socket_when_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: _probes(["dbus"]))
    monkeypatch.setattr(session_mod, "_extension_socket_installed", lambda: False)
    mgr = LiveSessionManager(_settings(tmp_path, enabled=True))

    status = mgr.status()
    assert status.available_transports == ["dbus"]


def test_available_transports_no_duplicate_extension_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When the probe ALREADY reports extension-socket available, the reconciliation must not add a
    # duplicate entry.
    monkeypatch.setattr(
        session_mod, "probe_transports", lambda settings=None: _probes(["extension-socket"])
    )
    monkeypatch.setattr(session_mod, "_extension_socket_installed", lambda: True)
    mgr = LiveSessionManager(_settings(tmp_path, enabled=True))

    status = mgr.status()
    assert status.available_transports.count("extension-socket") == 1
