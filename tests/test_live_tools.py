"""`live` and `live_edit`: auto-connect, observe parts, viewport, approval, observe-after-edit."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_LIVE_ENABLED, get_settings
from inkscape_mcp.live import session as session_mod
from inkscape_mcp.live.session import get_session_manager, reset_session_manager
from inkscape_mcp.live.transport import TransportProbe
from tests.conftest import FakeTransport
from tests.tool_support import call


@pytest.fixture
def fake(tool_ws, monkeypatch) -> FakeTransport:
    """Live enabled with ONE FakeTransport that `connect` will hand out."""
    monkeypatch.setenv(ENV_LIVE_ENABLED, "1")
    get_settings.cache_clear()
    reset_session_manager()
    transport = FakeTransport()
    monkeypatch.setattr(
        session_mod,
        "probe_transports",
        lambda settings=None: [
            TransportProbe(name="fake", available=True, rank=1, supported_commands=[], detail="")
        ],
    )
    monkeypatch.setattr(session_mod, "select_transport", lambda s, required: transport)
    yield transport
    reset_session_manager()


def _live(app, **args):
    return call(app, "live", args)


def test_observe_connects_automatically_and_returns_the_parts(tool_app, fake) -> None:
    assert not get_session_manager().status().connected
    res = _live(tool_app, action="observe", include=["document", "selection", "inspect", "scene"])
    assert get_session_manager().status().connected
    assert res.data["selection"]["object_ids"] == ["r1"]
    assert res.data["document"]["name"] == "live.svg"
    assert res.data["inspect"]["count"] == 1
    assert res.data["scene"]["selection_count"] == 1
    assert res.images == []


def test_observe_defaults_to_the_selection(tool_app, fake) -> None:
    assert list(_live(tool_app, action="observe").data) == ["selection"]


def test_fast_applies_the_loop_downscale_and_explicit_scale_wins(tool_app, fake) -> None:
    _live(tool_app, action="observe", include=["view"], fast=True)
    assert fake.last_render is not None and fake.last_render[1] == 0.5
    _live(tool_app, action="observe", include=["view"], fast=True, scale=1.0)
    assert fake.last_render[1] == 1.0


def test_connect_reuses_an_existing_session(tool_app, fake) -> None:
    first = _live(tool_app, action="connect").data
    assert first["connected"] is True
    second = _live(tool_app, action="connect").data
    assert second["connected_at"] == first["connected_at"]
    assert _live(tool_app, action="status").data["connected"] is True
    assert _live(tool_app, action="disconnect").data["connected"] is False


def test_check_and_status_do_not_connect(tool_app, fake) -> None:
    _live(tool_app, action="check")
    assert _live(tool_app, action="status").data["connected"] is False


def test_export_selection_attaches_a_png(tool_app, fake) -> None:
    res = _live(tool_app, action="export_selection")
    assert len(res.images) == 1


def test_actions_name_their_missing_argument(tool_app, fake) -> None:
    with pytest.raises(ToolError, match="requires operation_id"):
        _live(tool_app, action="diff")
    with pytest.raises(ToolError, match="requires mode"):
        _live(tool_app, action="viewport")


# --- live_edit ---------------------------------------------------------------


def _edit(app, **args):
    return call(app, "live_edit", args)


def test_live_edit_observe_render_attaches_a_png(tool_app, fake) -> None:
    res = _edit(
        tool_app,
        action="apply",
        transform={"dx": 5, "dy": 0},
        observe="render",
        approval_token="ok",
    )
    assert len(res.images) == 1


def test_live_edit_argument_validation(tool_app, fake) -> None:
    with pytest.raises(ToolError, match="at least one style or transform"):
        _edit(tool_app, action="apply", approval_token="ok")
    with pytest.raises(ToolError, match="takes style and/or transform only"):
        _edit(tool_app, action="apply", style={"fill": "#fff"}, text="x", approval_token="ok")
    with pytest.raises(ToolError, match="requires svg_fragment"):
        _edit(tool_app, action="insert_svg", approval_token="ok")
    with pytest.raises(ToolError, match="requires text"):
        _edit(tool_app, action="set_text", approval_token="ok")
    with pytest.raises(ToolError, match="does not take style"):
        _edit(tool_app, action="set_text", text="x", style={"fill": "#fff"}, approval_token="ok")
    # nothing above should have needed a connection
    assert not get_session_manager().status().connected
