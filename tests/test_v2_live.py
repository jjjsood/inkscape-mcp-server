"""`live` and `live_edit`: auto-connect, observe parts, viewport, approval, observe-after-edit."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_LIVE_ENABLED, get_settings
from inkscape_mcp.live import session as session_mod
from inkscape_mcp.live.session import get_session_manager, reset_session_manager
from inkscape_mcp.live.transport import TransportProbe
from tests.conftest import FakeTransport
from tests.v2_support import call


@pytest.fixture
def fake(v2_ws, monkeypatch) -> FakeTransport:
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


def test_observe_connects_automatically_and_returns_the_parts(v2_app, fake) -> None:
    assert not get_session_manager().status().connected
    res = _live(v2_app, action="observe", include=["document", "selection", "inspect", "scene"])
    assert get_session_manager().status().connected
    assert res.data["selection"]["object_ids"] == ["r1"]
    assert res.data["document"]["name"] == "live.svg"
    assert res.data["inspect"]["count"] == 1
    assert res.data["scene"]["selection_count"] == 1
    assert res.images == []


def test_observe_defaults_to_the_selection(v2_app, fake) -> None:
    assert list(_live(v2_app, action="observe").data) == ["selection"]


def test_observe_view_attaches_a_png_and_forwards_region_and_scale(v2_app, fake) -> None:
    res = _live(
        v2_app,
        action="observe",
        include=["view"],
        region={"x": 0, "y": 0, "width": 5, "height": 5},
        scale=0.5,
    )
    assert len(res.images) == 1
    assert fake.last_render is not None and fake.last_render[1] == 0.5
    assert "artifact_path" not in res.data


def test_fast_applies_the_loop_downscale_and_explicit_scale_wins(v2_app, fake) -> None:
    _live(v2_app, action="observe", include=["view"], fast=True)
    assert fake.last_render is not None and fake.last_render[1] == 0.5
    _live(v2_app, action="observe", include=["view"], fast=True, scale=1.0)
    assert fake.last_render[1] == 1.0


def test_connect_reuses_an_existing_session(v2_app, fake) -> None:
    first = _live(v2_app, action="connect").data
    assert first["connected"] is True
    second = _live(v2_app, action="connect").data
    assert second["connected_at"] == first["connected_at"]
    assert _live(v2_app, action="status").data["connected"] is True
    assert _live(v2_app, action="disconnect").data["connected"] is False


def test_check_and_status_do_not_connect(v2_app, fake) -> None:
    _live(v2_app, action="check")
    assert _live(v2_app, action="status").data["connected"] is False


def test_viewport_and_wait(v2_app, fake) -> None:
    res = _live(v2_app, action="viewport", mode="zoom", zoom=2.0)
    assert res.data["applied"] is True and fake.last_viewport["mode"] == "zoom"
    with pytest.raises(ToolError, match="requires mode"):
        _live(v2_app, action="viewport")
    waited = _live(v2_app, action="wait", timeout_s=0.0)
    assert "changed" in waited.data or "timed_out" in waited.data
    with pytest.raises(ToolError, match="timeout_s"):
        _live(v2_app, action="wait", timeout_s=999)


def test_export_selection_attaches_a_png(v2_app, fake) -> None:
    res = _live(v2_app, action="export_selection")
    assert len(res.images) == 1


def test_diff_requires_an_operation_id(v2_app, fake) -> None:
    with pytest.raises(ToolError, match="requires operation_id"):
        _live(v2_app, action="diff")


def test_bad_region_is_a_validation_error(v2_app, fake) -> None:
    with pytest.raises(ToolError):
        _live(
            v2_app,
            action="observe",
            include=["view"],
            region={"x": 0, "y": 0, "width": -1, "height": 5},
        )


def test_live_disabled_is_reported_with_the_next_step(v2_app, v2_ws, monkeypatch) -> None:
    monkeypatch.setenv(ENV_LIVE_ENABLED, "0")
    get_settings.cache_clear()
    reset_session_manager()
    with pytest.raises(ToolError, match="live mode is disabled"):
        _live(v2_app, action="observe")


# --- live_edit ---------------------------------------------------------------


def _edit(app, **args):
    return call(app, "live_edit", args)


def test_live_edit_apply_needs_approval(v2_app, fake) -> None:
    with pytest.raises(ToolError, match="approval"):
        _edit(v2_app, action="apply", style={"fill": "#ff0000"})


def test_live_edit_apply_connects_and_reports_the_operation(v2_app, fake) -> None:
    res = _edit(v2_app, action="apply", style={"fill": "#ff0000"}, approval_token="ok")
    assert get_session_manager().status().connected
    assert res.data["operation_id"] and res.data["affected_ids"] == ["r1"]
    assert res.images == []


def test_live_edit_observe_render_attaches_a_png(v2_app, fake) -> None:
    res = _edit(
        v2_app,
        action="apply",
        transform={"dx": 5, "dy": 0},
        observe="render",
        approval_token="ok",
    )
    assert len(res.images) == 1


def test_live_edit_observe_diff(v2_app, fake) -> None:
    res = _edit(
        v2_app, action="apply", style={"fill": "#00ff00"}, observe="diff", approval_token="ok"
    )
    assert res.data["operation_id"]
    # the diff overlay is best effort; when it exists it comes back as an image
    assert len(res.images) <= 1


def test_live_edit_insert_svg_and_set_text(v2_app, fake) -> None:
    ins = _edit(
        v2_app,
        action="insert_svg",
        svg_fragment='<circle xmlns="http://www.w3.org/2000/svg" id="c1" cx="1" cy="1" r="1"/>',
        approval_token="ok",
    )
    assert ins.data["operation_id"]
    txt = _edit(v2_app, action="set_text", text="hello", approval_token="ok")
    assert txt.data["operation_id"]


def test_live_edit_argument_validation(v2_app, fake) -> None:
    with pytest.raises(ToolError, match="at least one style or transform"):
        _edit(v2_app, action="apply", approval_token="ok")
    with pytest.raises(ToolError, match="takes style and/or transform only"):
        _edit(v2_app, action="apply", style={"fill": "#fff"}, text="x", approval_token="ok")
    with pytest.raises(ToolError, match="requires svg_fragment"):
        _edit(v2_app, action="insert_svg", approval_token="ok")
    with pytest.raises(ToolError, match="requires text"):
        _edit(v2_app, action="set_text", approval_token="ok")
    with pytest.raises(ToolError, match="does not take style"):
        _edit(v2_app, action="set_text", text="x", style={"fill": "#fff"}, approval_token="ok")
    # nothing above should have needed a connection
    assert not get_session_manager().status().connected


def test_open_from_live_saves_the_canvas_into_the_workspace(v2_app, v2_ws, fake) -> None:
    res = call(v2_app, "open", {"from_live": {"dest_path": "from-live.svg"}})
    assert res.data["saved_path"].endswith("from-live.svg")
    assert (v2_ws / "from-live.svg").is_file()
    assert get_session_manager().status().connected
