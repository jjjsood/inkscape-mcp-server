"""Extension-socket backend tests: rendezvous, loopback handshake, schema, install."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, Settings, get_settings
from inkscape_mcp.live.protocol import PROTOCOL_VERSION, LiveCommand
from inkscape_mcp.live.socket_backend import (
    ENV_RENDEZVOUS,
    ExtensionSocketTransport,
    discover_rendezvous,
    install_helper,
    is_helper_installed,
)
from inkscape_mcp.live.transport import LiveConnectionError

from .conftest import mock_helper


def _write_rendezvous(path: Path, port: int, token: str, version: int = PROTOCOL_VERSION) -> None:
    path.write_text(
        json.dumps({"port": port, "token": token, "protocol_version": version}),
        encoding="utf-8",
    )


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    return get_settings()


def test_dials_loopback_and_completes_handshake_and_reads(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "secret-token"
    with mock_helper(token=token) as port:
        rv = tmp_path / "rv.json"
        _write_rendezvous(rv, port, token)
        monkeypatch.setenv(ENV_RENDEZVOUS, str(rv))

        transport = ExtensionSocketTransport(settings)
        transport.connect()
        assert transport.is_connected()

        # The full read surface roundtrips through the real socket client.
        doc = transport.get_active_document()
        assert doc.name == "live.svg"
        assert doc.object_count == 2

        sel = transport.get_selection()
        assert sel.object_ids == ["a", "b"]
        assert sel.count == 2

        inspected = transport.inspect_selection()
        assert inspected.count == 1
        assert inspected.objects[0].id == "a"
        assert inspected.objects[0].tag == "rect"

        svg = transport.get_document_svg()
        assert svg.startswith("<svg")

        transport.disconnect()
        assert not transport.is_connected()


def test_bad_token_handshake_is_rejected(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with mock_helper(token="real-token") as port:
        rv = tmp_path / "rv.json"
        _write_rendezvous(rv, port, "WRONG-token")
        monkeypatch.setenv(ENV_RENDEZVOUS, str(rv))

        transport = ExtensionSocketTransport(settings)
        with pytest.raises(LiveConnectionError):
            transport.connect()
        assert not transport.is_connected()


def test_connect_without_session_degrades_cleanly(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point discovery at a path with no rendezvous file → no live session available.
    monkeypatch.setenv(ENV_RENDEZVOUS, str(tmp_path / "absent.json"))
    transport = ExtensionSocketTransport(settings)
    with pytest.raises(LiveConnectionError):
        transport.connect()


def test_discover_rendezvous_skips_malformed_and_version_mismatch(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rv = tmp_path / "rv.json"
    monkeypatch.setenv(ENV_RENDEZVOUS, str(rv))

    rv.write_text("not json", encoding="utf-8")
    assert discover_rendezvous(settings) is None

    _write_rendezvous(rv, 5000, "tok", version=PROTOCOL_VERSION + 1)
    assert discover_rendezvous(settings) is None

    _write_rendezvous(rv, 5000, "tok")
    found = discover_rendezvous(settings)
    assert found is not None and found.port == 5000


def test_probe_reports_available_when_rendezvous_present(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rv = tmp_path / "rv.json"
    monkeypatch.setenv(ENV_RENDEZVOUS, str(rv))

    probe = ExtensionSocketTransport.probe(settings)
    assert probe.available is False  # no file yet

    _write_rendezvous(rv, 4321, "tok")
    probe = ExtensionSocketTransport.probe(settings)
    assert probe.available is True
    assert LiveCommand.GET_SELECTION.value in probe.supported_commands


def test_set_viewport_and_region_render_roundtrip_over_socket(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64

    from inkscape_mcp.live.transport import RenderRegion

    seen: dict[str, dict] = {}

    def handler(cmd: str, params: dict) -> dict:
        if cmd == LiveCommand.SET_VIEWPORT:
            seen["viewport"] = params
            return {"mode": params.get("mode"), "applied": True}
        if cmd == LiveCommand.RENDER_VIEW:
            seen["render"] = params
            return {"png_base64": base64.b64encode(b"PNGDATA").decode("ascii")}
        raise KeyError(cmd)

    token = "secret-token"
    with mock_helper(token=token, handler=handler) as port:
        rv = tmp_path / "rv.json"
        _write_rendezvous(rv, port, token)
        monkeypatch.setenv(ENV_RENDEZVOUS, str(rv))

        transport = ExtensionSocketTransport(settings)
        transport.connect()

        # set_viewport carries only the fixed mode + bounded numerics across the wire.
        result = transport.set_viewport(mode="zoom", zoom=2.0, center=(1.0, 2.0))
        assert result.mode == "zoom"
        assert seen["viewport"] == {"mode": "zoom", "zoom": 2.0, "center": [1.0, 2.0]}

        # render_view forwards the region (as a flat list) and scale.
        png = transport.render_view(region=RenderRegion(x=0, y=0, width=4, height=4), scale=0.5)
        assert png == b"PNGDATA"
        assert seen["render"] == {"region": [0.0, 0.0, 4.0, 4.0], "scale": 0.5}

        # Whole-canvas render sends no region/scale params (backward-compatible).
        transport.render_view()
        assert seen["render"] == {}

        transport.disconnect()


def test_install_helper_copies_both_files_and_is_detected(tmp_path: Path) -> None:
    ext_dir = tmp_path / "extensions"
    installed = install_helper(ext_dir)
    assert set(installed) == {"inkscape_mcp_live.py", "inkscape_mcp_live.inx"}
    assert (ext_dir / "inkscape_mcp_live.py").is_file()
    assert (ext_dir / "inkscape_mcp_live.inx").is_file()
    # The data dir's parent is what `is_helper_installed` scans (it appends "extensions/").
    assert is_helper_installed([str(tmp_path)]) is True
    assert is_helper_installed([str(tmp_path / "nowhere")]) is False
