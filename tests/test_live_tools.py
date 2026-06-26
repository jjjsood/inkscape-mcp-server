"""Live tool + resource tests (05/06): registration, gate default-on/opt-out, absence."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_LIVE_ENABLED, ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.live import session as session_mod
from inkscape_mcp.live.session import LiveSession, reset_session_manager
from inkscape_mcp.live.transport import LiveSelection, TransportProbe
from inkscape_mcp.resources.live import live_selection, live_session
from inkscape_mcp.server import mcp
from inkscape_mcp.tools import live as live_tools
from inkscape_mcp.tools.live import (
    LiveSupport,
    check_live_support,
    live_connect,
    live_get_active_document,
    live_get_selection,
    live_inspect_selection,
    live_status,
)

from .conftest import FakeTransport


def _tool_names() -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def _resource_uris() -> set[str]:
    return {str(r.uri) for r in asyncio.run(mcp.list_resources())}


def test_live_tools_and_resources_registered() -> None:
    names = _tool_names()
    for expected in (
        "check_live_support",
        "live_connect",
        "live_disconnect",
        "live_status",
        "live_get_active_document",
        "live_get_selection",
        "live_inspect_selection",
        "live_render_view",
        "live_sync_to_workspace",
        "live_install_helper",
        "live_apply_to_selection",
        "live_insert_svg",
        "live_set_selected_text",
        "live_export_selection",
        "live_set_viewport",
        "live_get_scene",
        "live_wait_for_change",
        "live_diff_view",
        "live_session_step",
    ):
        assert expected in names
    uris = _resource_uris()
    assert "inkscape://live/session" in uris
    assert "inkscape://live/selection" in uris
    assert "inkscape://live/operations" in uris
    assert "inkscape://live/view" in uris
    assert "inkscape://live/events" in uris


@pytest.fixture
def off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live explicitly disabled, one available transport reported, clean singleton."""
    monkeypatch.setenv(ENV_LIVE_ENABLED, "0")
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    reset_session_manager()
    monkeypatch.setattr(
        session_mod,
        "probe_transports",
        lambda settings=None: [
            TransportProbe(name="fake", available=True, rank=1, supported_commands=[], detail="")
        ],
    )
    monkeypatch.setattr(live_tools, "probe_transports", lambda settings=None: [], raising=True)
    monkeypatch.setattr(live_tools, "best_available", lambda s, required: None, raising=True)
    monkeypatch.setattr(live_tools, "is_helper_installed", lambda data_dirs: False, raising=True)


@pytest.fixture
def on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live enabled + a FakeTransport selectable, clean singleton."""
    monkeypatch.setenv(ENV_LIVE_ENABLED, "1")
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    reset_session_manager()
    monkeypatch.setattr(
        session_mod,
        "probe_transports",
        lambda settings=None: [
            TransportProbe(name="fake", available=True, rank=1, supported_commands=[], detail="")
        ],
    )
    monkeypatch.setattr(session_mod, "select_transport", lambda s, required: FakeTransport())


def test_live_enabled_on_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator-chosen X1 default: the live gate is ON when the env override is unset."""
    monkeypatch.delenv(ENV_LIVE_ENABLED, raising=False)
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    assert get_settings().live_enabled is True


def test_check_live_support_reports_disabled_when_off(off: None) -> None:
    support = check_live_support()
    assert isinstance(support, LiveSupport)
    assert support.live_enabled is False
    assert any("disabled" in n for n in support.notes)


def test_live_status_clean_when_not_connected(off: None) -> None:
    status = live_status()
    assert status.connected is False
    assert status.enabled is False


def test_live_connect_refused_when_disabled(off: None) -> None:
    with pytest.raises(ToolError):
        live_connect()


def test_read_tools_error_cleanly_without_session(off: None) -> None:
    for call in (live_get_active_document, live_get_selection, live_inspect_selection):
        with pytest.raises(ToolError):
            call()


def test_session_resource_reports_not_connected(off: None) -> None:
    assert LiveSession.model_validate_json(live_session()).connected is False


def test_selection_resource_empty_without_session(off: None) -> None:
    sel = LiveSelection.model_validate_json(live_selection())
    assert sel.object_ids == []
    assert sel.count == 0


def test_connect_then_read_surface(on: None) -> None:
    session = live_connect()
    assert session.connected is True
    assert session.transport == "fake"

    assert live_get_active_document().name == "live.svg"
    assert live_get_selection().object_ids == ["r1"]
    assert live_inspect_selection().objects[0].tag == "rect"

    # Resources reflect the live session.
    assert LiveSession.model_validate_json(live_session()).connected is True
    assert LiveSelection.model_validate_json(live_selection()).object_ids == ["r1"]


# --- L5: live_install_helper returns a non-absolute extensions_dir -----------


def test_present_extensions_dir_collapses_home_prefix() -> None:
    from inkscape_mcp.tools.live import _present_extensions_dir

    home = Path.home()
    presented = _present_extensions_dir(home / ".config" / "inkscape" / "extensions")
    assert presented.startswith("~/")
    assert not Path(presented).is_absolute()
    assert str(home) not in presented


def test_present_extensions_dir_outside_home_uses_basename() -> None:
    from inkscape_mcp.tools.live import _present_extensions_dir

    presented = _present_extensions_dir(Path("/opt/inkscape/share/extensions"))
    assert not Path(presented).is_absolute()
    assert presented == "extensions"
    assert "/opt/inkscape" not in presented


def test_live_install_helper_returns_relative_extensions_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    import inkscape_mcp.tools.live as live_mod
    from inkscape_mcp.tools.live import HelperInstallResult, live_install_helper

    monkeypatch.setenv(ENV_LIVE_ENABLED, "1")
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()

    # Pretend the Inkscape user data dir lives under the (real) home so the result is `~`-relative.
    user_data = Path.home() / ".inkscape-mcp-test-data"
    monkeypatch.setattr(
        live_mod,
        "get_settings",
        lambda: SimpleNamespace(live_enabled=True),
    )
    import inkscape_mcp.tools.system as system_mod

    monkeypatch.setattr(
        system_mod,
        "get_cached_capabilities",
        lambda: SimpleNamespace(user_data_dir=str(user_data)),
    )
    monkeypatch.setattr(
        "inkscape_mcp.live.socket_backend.install_helper",
        lambda extensions_dir: ["inkscape_mcp_live.py", "inkscape_mcp_live.inx"],
    )

    result = live_install_helper()
    assert isinstance(result, HelperInstallResult)
    assert result.installed_files == ["inkscape_mcp_live.py", "inkscape_mcp_live.inx"]
    assert not Path(result.extensions_dir).is_absolute()
    assert result.extensions_dir.startswith("~/")
    assert str(Path.home()) not in result.extensions_dir


# ---: capability-absent live errors name the connect / probe tools ----


def test_map_live_error_not_available_names_live_connect() -> None:
    from inkscape_mcp.live.transport import LiveNotAvailable
    from inkscape_mcp.tools.live import _map_live_error

    msg = str(_map_live_error(LiveNotAvailable("none")))
    assert "live_connect" in msg
    assert "check_live_support" in msg


def test_map_live_error_disabled_names_check_live_support() -> None:
    from inkscape_mcp.live.transport import LiveDisabled
    from inkscape_mcp.tools.live import _map_live_error

    msg = str(_map_live_error(LiveDisabled("off")))
    assert "check_live_support" in msg


def test_map_live_error_unsupported_capability_names_check_live_support() -> None:
    from inkscape_mcp.live.transport import LiveCapabilityUnsupported
    from inkscape_mcp.tools.live import _map_live_error

    msg = str(_map_live_error(LiveCapabilityUnsupported("no")))
    assert "check_live_support" in msg
