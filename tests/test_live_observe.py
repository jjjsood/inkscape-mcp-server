"""`live` read/steer side: setup actions, observe parts, viewport, error map, helper arming.

Complements `test_live_tools.py` (auto-connect, view PNG, fast scale, connect reuse): this file
holds the validators and view-only engine behind `observe`/`viewport`, the live-disabled and
no-session paths, `install_helper` / `arm_socket`, and the read-only session resources.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_LIVE_ENABLED, ENV_WORKSPACE_ROOTS, Settings, get_settings
from inkscape_mcp.edit.dom import EditError
from inkscape_mcp.live import admin as admin_mod
from inkscape_mcp.live import session as session_mod
from inkscape_mcp.live import socket_backend
from inkscape_mcp.live.admin import LiveSupport
from inkscape_mcp.live.edit import (
    set_live_viewport,
    validate_viewport,
)
from inkscape_mcp.live.records import list_live_operations
from inkscape_mcp.live.session import LiveSession, LiveSessionManager, reset_session_manager
from inkscape_mcp.live.transport import (
    LiveCapabilityUnsupported,
    LiveConnectionError,
    LiveDisabled,
    LiveError,
    LiveNotAvailable,
    LiveSelection,
    RenderRegion,
    TransportProbe,
)
from inkscape_mcp.resources.live import live_selection, live_session
from inkscape_mcp.runtime import capabilities as capabilities_mod
from inkscape_mcp.runtime.probe import Capabilities
from inkscape_mcp.tools._result import to_tool_error
from tests.conftest import FakeTransport
from tests.tool_support import call

FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKE-CANVAS"
_PROBE = TransportProbe(name="fake", available=True, rank=1, supported_commands=[], detail="")


def _configure(monkeypatch: pytest.MonkeyPatch, ws: Path, *, enabled: bool) -> None:
    monkeypatch.setenv(ENV_LIVE_ENABLED, "1" if enabled else "0")
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_session_manager()


@pytest.fixture
def on(tool_ws, monkeypatch: pytest.MonkeyPatch) -> FakeTransport:
    """Live enabled; `connect` hands out one FakeTransport (returned for inspection)."""
    _configure(monkeypatch, tool_ws, enabled=True)
    transport = FakeTransport(png=FAKE_PNG)
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [_PROBE])
    monkeypatch.setattr(session_mod, "select_transport", lambda s, required: transport)
    yield transport
    reset_session_manager()


@pytest.fixture
def off(tool_ws, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live disabled, one transport reported, helper not installed."""
    _configure(monkeypatch, tool_ws, enabled=False)
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [_PROBE])
    monkeypatch.setattr(admin_mod, "probe_transports", lambda settings=None: [])
    monkeypatch.setattr(admin_mod, "best_available", lambda s, required: None)
    monkeypatch.setattr(admin_mod, "is_helper_installed", lambda data_dirs: False)
    yield
    reset_session_manager()


def _live(app, **args):  # type: ignore[no-untyped-def]
    return call(app, "live", args)


# --- live disabled / no session -----------------------------------------------


def test_live_gate_defaults_to_on(tool_ws, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_LIVE_ENABLED, raising=False)
    get_settings.cache_clear()
    assert get_settings().live_enabled is True


def test_check_and_status_report_a_disabled_gate_cleanly(tool_app, off) -> None:
    check = _live(tool_app, action="check").data
    assert check["live_enabled"] is False
    assert any("disabled" in n for n in check["notes"])
    LiveSupport.model_validate(check)
    status = _live(tool_app, action="status").data
    assert status["connected"] is False and status["enabled"] is False


@pytest.mark.parametrize("action", ["connect", "observe", "wait", "export_selection"])
def test_live_actions_refuse_when_disabled_with_the_next_step(tool_app, off, action: str) -> None:
    with pytest.raises(ToolError, match=r"live mode is disabled.*live\(action='check'\)"):
        _live(tool_app, action=action)


@pytest.mark.parametrize("action", ["install_helper", "arm_socket"])
def test_helper_actions_refuse_when_disabled(tool_app, off, action: str) -> None:
    with pytest.raises(ToolError, match="live mode is disabled"):
        _live(tool_app, action=action)


def test_observe_without_any_transport_names_connect(tool_app, tool_ws, monkeypatch) -> None:
    _configure(monkeypatch, tool_ws, enabled=True)
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    monkeypatch.setattr(session_mod, "select_transport", lambda s, required, **kw: None)
    with pytest.raises(ToolError, match=r"no live session available.*live\(action='connect'\)"):
        _live(tool_app, action="observe")
    with pytest.raises(ToolError, match="no live session available"):
        _live(tool_app, action="connect", prefer="no_freeze")
    assert _live(tool_app, action="status").data["connected"] is False


@pytest.mark.parametrize(
    ("exc", "needle"),
    [
        (LiveDisabled("off"), r"live\(action='check'\)"),
        (LiveNotAvailable("none"), r"live\(action='connect'\)"),
        (LiveCapabilityUnsupported("no"), r"live\(action='check'\)"),
        (
            LiveConnectionError("/home/u/secret.sock refused"),
            r"^live session communication failed$",
        ),
        (LiveError("/home/u/secret detail"), r"^live operation failed$"),
    ],
)
def test_live_errors_map_to_actionable_host_path_free_messages(exc: Exception, needle: str) -> None:
    message = str(to_tool_error(exc))
    assert message and "/home" not in message
    assert re.search(needle, message)


# --- connect, read surface, resources -----------------------------------------


def test_connect_then_read_surface_and_resources(tool_app, on) -> None:
    assert LiveSession.model_validate_json(live_session()).connected is False
    assert LiveSelection.model_validate_json(live_selection()).object_ids == []

    connected = _live(tool_app, action="connect").data
    assert connected["connected"] is True and connected["transport"] == "fake"
    parts = _live(tool_app, action="observe", include=["document", "selection", "inspect"]).data
    assert parts["document"]["name"] == "live.svg"
    assert parts["selection"]["object_ids"] == ["r1"]
    assert parts["inspect"]["objects"][0]["tag"] == "rect"

    assert LiveSession.model_validate_json(live_session()).connected is True
    assert LiveSelection.model_validate_json(live_selection()).object_ids == ["r1"]
    _live(tool_app, action="disconnect")
    assert LiveSelection.model_validate_json(live_selection()).count == 0


def test_observe_is_read_only_and_records_nothing(tool_app, on) -> None:
    _live(tool_app, action="observe", include=["document", "selection", "inspect", "scene", "view"])
    _live(tool_app, action="export_selection")
    assert list_live_operations(settings=get_settings()).count == 0


# --- observe(view): region / scale --------------------------------------------


def test_observe_view_reports_region_and_scale_and_whole_canvas_has_neither(tool_app, on) -> None:
    res = _live(
        tool_app,
        action="observe",
        include=["view"],
        region={"x": 0, "y": 0, "width": 4, "height": 4},
        scale=0.5,
    )
    assert res.data["view"] == {"scale": 0.5, "region": True}
    assert on.last_render == (RenderRegion(x=0, y=0, width=4, height=4), 0.5)
    whole = _live(tool_app, action="observe", include=["view"])
    assert len(whole.images) == 1 and "view" not in whole.data
    assert on.last_render == (None, None)


@pytest.mark.parametrize(
    "args",
    [
        {"region": {"x": 0.0}},  # all four parts are required together
        {"region": {"x": 0, "y": 0, "width": 0, "height": 5}},
        {"region": {"x": 0, "y": 0, "width": 5, "height": 5, "extra": 1}},
        {"region": {"x": 0, "y": 0, "width": 5, "height": float("nan")}},
        {"scale": 0},
        {"scale": 1e6},
    ],
    ids=["partial", "zero-width", "extra-field", "nan-height", "scale-0", "scale-huge"],
)
def test_observe_view_rejects_a_bad_frame_request(tool_app, on, args: dict) -> None:
    with pytest.raises(ToolError):
        _live(tool_app, action="observe", include=["view"], **args)
    assert on.last_render is None


# --- viewport -----------------------------------------------------------------


def test_viewport_validator_per_mode() -> None:
    assert validate_viewport("zoom", zoom=2.0) == {"mode": "zoom", "zoom": 2.0}
    assert validate_viewport("zoom", zoom=2.0, center_x=1.0, center_y=3.0) == {
        "mode": "zoom",
        "zoom": 2.0,
        "center": (1.0, 3.0),
    }
    assert validate_viewport("pan", dx=5.0, dy=-2.0) == {"mode": "pan", "dx": 5.0, "dy": -2.0}
    assert validate_viewport("fit_selection") == {"mode": "fit_selection"}
    assert validate_viewport("fit_page") == {"mode": "fit_page"}


@pytest.mark.parametrize(
    ("mode", "kw"),
    [
        ("orbit", {}),  # not a fixed mode
        ("zoom", {}),  # zoom needs a factor
        ("zoom", {"zoom": 0}),
        ("zoom", {"zoom": float("inf")}),
        ("zoom", {"zoom": 2.0, "center_x": 1.0}),  # centre needs both
        ("pan", {"dx": 1.0}),  # pan needs both
        ("pan", {"dx": 1.0, "dy": 1e12}),  # out of bounds
    ],
)
def test_viewport_rejects_bad_input_in_engine_and_tool(tool_app, on, mode: str, kw: dict) -> None:
    with pytest.raises(EditError):
        validate_viewport(mode, **kw)
    with pytest.raises(ToolError):
        _live(tool_app, action="viewport", mode=mode, **kw)
    assert on.last_viewport is None


def test_viewport_modes_drive_the_transport_without_approval_or_record(tool_app, on) -> None:
    for mode, kw in [
        ("fit_page", {}),
        ("fit_selection", {}),
        ("zoom", {"zoom": 3.0}),
        ("pan", {"dx": 2.0, "dy": 4.0}),
    ]:
        assert _live(tool_app, action="viewport", mode=mode, **kw).data["mode"] == mode
    zoomed = _live(tool_app, action="viewport", mode="zoom", zoom=2.0, center_x=1.0, center_y=2.0)
    assert zoomed.data["applied"] is True
    assert on.last_viewport == {
        "mode": "zoom",
        "zoom": 2.0,
        "center": (1.0, 2.0),
        "dx": None,
        "dy": None,
    }
    assert list_live_operations(settings=get_settings()).count == 0


def test_viewport_on_a_transport_without_the_command_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class NoViewport(FakeTransport):
        supported_commands = FakeTransport.supported_commands - {
            socket_backend.LiveCommand.SET_VIEWPORT
        }

    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    monkeypatch.setattr(session_mod, "select_transport", lambda s, required: NoViewport())
    mgr = LiveSessionManager(Settings(workspace_roots=[tmp_path], live_enabled=True))
    mgr.connect()
    with pytest.raises(LiveCapabilityUnsupported):
        set_live_viewport(validate_viewport("fit_page"), manager=mgr)


# --- install_helper / arm_socket ----------------------------------------------


@pytest.mark.parametrize(
    ("user_data", "home_relative"),
    [(Path.home() / ".inkscape-mcp-test-data", True), (Path("/opt/inkscape/share"), False)],
    ids=["under-home", "outside-home"],
)
def test_install_helper_returns_a_non_absolute_extensions_dir(
    tool_app, on, monkeypatch: pytest.MonkeyPatch, user_data: Path, home_relative: bool
) -> None:
    monkeypatch.setattr(
        capabilities_mod,
        "get_cached_capabilities",
        lambda: SimpleNamespace(user_data_dir=str(user_data)),
    )
    monkeypatch.setattr(
        socket_backend,
        "install_helper",
        lambda extensions_dir: ["inkscape_mcp_live.py", "inkscape_mcp_live.inx"],
    )
    res = _live(tool_app, action="install_helper").data
    assert res["installed_files"] == ["inkscape_mcp_live.py", "inkscape_mcp_live.inx"]
    if home_relative:
        assert res["extensions_dir"].startswith("~/")
    else:
        assert res["extensions_dir"] == "extensions"
    assert str(Path.home()) not in res["extensions_dir"]


def _arming(monkeypatch: pytest.MonkeyPatch, ws: Path, *, advertised: bool) -> None:
    caps = Capabilities.model_construct(user_data_dir=str(ws / "inkscape"), system_data_dir=None)
    monkeypatch.setattr(capabilities_mod, "get_cached_capabilities", lambda: caps)
    monkeypatch.setattr(admin_mod, "is_helper_installed", lambda dirs: True)
    rv = socket_backend.Rendezvous(port=5000, token="tok")
    monkeypatch.setattr(socket_backend, "discover_rendezvous", lambda s: rv if advertised else None)


def test_arm_socket_reuses_an_already_armed_session(tool_app, on, tool_ws, monkeypatch) -> None:
    _arming(monkeypatch, tool_ws, advertised=True)
    res = _live(tool_app, action="arm_socket").data
    assert res["armed"] is True and res["launched"] is False and res["helper_installed"] is True


def test_arm_socket_launches_when_nothing_is_advertised(
    tool_app, on, tool_ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arming(monkeypatch, tool_ws, advertised=False)
    monkeypatch.setattr(
        admin_mod, "arm_socket_helper", lambda s: socket_backend.Rendezvous(port=1, token="t")
    )
    res = _live(tool_app, action="arm_socket").data
    assert res["armed"] is True and res["launched"] is True


def test_arm_socket_headless_fails_clearly_instead_of_launching(
    tool_app, on, tool_ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arming(monkeypatch, tool_ws, advertised=False)
    monkeypatch.setattr(socket_backend.shutil, "which", lambda name: "/usr/bin/inkscape")
    monkeypatch.setattr(socket_backend, "_display_available", lambda: False)
    spawned: list[object] = []
    monkeypatch.setattr(socket_backend.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    with pytest.raises(ToolError, match="display"):
        _live(tool_app, action="arm_socket")
    with pytest.raises(socket_backend.SocketArmError, match=r"(?i)display"):
        socket_backend.arm_socket_helper(get_settings())
    assert spawned == []


def test_arm_launch_argv_is_a_fixed_arglist() -> None:
    argv = socket_backend._arm_launch_argv("/usr/bin/inkscape", Path("/tmp/doc.svg"))
    assert argv == [
        "/usr/bin/inkscape",
        "--with-gui",
        f"--actions={socket_backend.HELPER_ACTION_ID}",
        "/tmp/doc.svg",
    ]
