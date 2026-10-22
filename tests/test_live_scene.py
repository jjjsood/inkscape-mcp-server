"""Structured scene perception: `LiveScene` assembly, `observe(include=[scene, view])`, view.

`live(action=observe, include=['scene','view'])` replaces the old frame tool: the structured scene
and the inline PNG arrive in one call, read-only and without an Operation Record.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inkscape_mcp.config import ENV_LIVE_ENABLED, ENV_WORKSPACE_ROOTS, Settings, get_settings
from inkscape_mcp.document.inspect import ObjectInfo
from inkscape_mcp.live import session as session_mod
from inkscape_mcp.live.protocol import PROTOCOL_VERSION, LiveCommand
from inkscape_mcp.live.scene import get_live_scene, scene_from_result
from inkscape_mcp.live.session import LiveSessionManager, reset_session_manager
from inkscape_mcp.live.socket_backend import ENV_RENDEZVOUS, ExtensionSocketTransport
from inkscape_mcp.live.transport import BBox, LiveDocumentRef, LiveScene
from inkscape_mcp.resources.live import live_view
from tests.conftest import FakeTransport, mock_helper
from tests.tool_support import call

FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKE-CANVAS"


@pytest.fixture
def live_on(tool_ws, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Live on; `connect` hands out a FakeTransport. Returns the workspace root."""
    monkeypatch.setenv(ENV_LIVE_ENABLED, "1")
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tool_ws))
    get_settings.cache_clear()
    reset_session_manager()
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    monkeypatch.setattr(
        session_mod, "select_transport", lambda s, required: FakeTransport(png=FAKE_PNG)
    )
    yield tool_ws
    reset_session_manager()


# --- coercion of the raw get_scene payload ------------------------------------


def test_scene_from_result_assembles_a_typed_scene() -> None:
    raw = {
        "selection": [
            {"id": "r1", "bbox": [1.0, 2.0, 3.0, 4.0]},
            {"id": "r2", "bbox": None},
            {"id": None, "bbox": [0, 0, 1, 1]},  # dropped: no id
        ],
        "viewport": {"zoom": 2.0, "center": [5.0, 6.0], "visible_region": [0, 0, 10, 10]},
        "canvas": {"width": 100.0, "height": 50.0, "units": "mm"},
        "visible_objects": [
            {"id": "r1", "tag": "rect", "label": "L", "has_style": True},
            {"id": "r2", "tag": "circle", "label": None, "has_style": False},
        ],
    }
    doc = LiveDocumentRef(name="live.svg", path="/u/live.svg", object_count=2)
    scene = scene_from_result(raw, doc)
    assert scene.active_document == doc
    assert scene.selection_count == 2  # the id-less entry is dropped
    assert scene.selection[0].bbox == BBox(x=1.0, y=2.0, width=3.0, height=4.0)
    assert scene.selection[1].bbox is None
    assert scene.viewport.zoom == 2.0 and scene.viewport.center == (5.0, 6.0)
    assert scene.viewport.visible_region == BBox(x=0, y=0, width=10, height=10)
    assert scene.canvas.width == 100.0 and scene.canvas.units == "mm"
    assert scene.object_count == 2
    # visible objects reuse the headless inspect `ObjectInfo` model
    assert scene.visible_objects[0] == ObjectInfo(id="r1", tag="rect", label="L", has_style=True)


def test_scene_from_result_rejects_non_finite_and_malformed_values() -> None:
    raw = {
        "selection": "not-a-list",
        "viewport": {"zoom": float("inf"), "center": ["x", "y"]},
        "canvas": {"width": float("nan"), "height": None},
        "visible_objects": [{"id": "r1", "tag": "rect"}, "junk"],
    }
    scene = scene_from_result(raw, None)
    assert scene.selection == []
    assert scene.viewport.zoom is None and scene.viewport.center is None
    assert scene.canvas.width is None
    assert scene.object_count == 1  # the "junk" entry is skipped


def test_get_live_scene_pulls_from_the_connected_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    monkeypatch.setattr(session_mod, "select_transport", lambda s, required: FakeTransport())
    mgr = LiveSessionManager(Settings(workspace_roots=[tmp_path], live_enabled=True))
    mgr.connect()
    scene = get_live_scene(manager=mgr)
    assert scene.selection[0].id == "r1" and scene.canvas.width == 10.0
    assert scene.visible_objects[0].tag == "rect"


# --- observe(include=[scene, view]) -------------------------------------------


def test_observe_scene_and_view_come_back_in_one_call(tool_app, live_on) -> None:
    res = call(tool_app, "live", {"action": "observe", "include": ["scene", "view"]})
    scene = res.data["scene"]  # compacted JSON: empty/None fields are dropped
    assert scene["selection_count"] == 1 and scene["canvas"]["width"] == 10.0
    assert scene["selection"][0]["id"] == "r1" and scene["visible_objects"][0]["tag"] == "rect"
    assert len(res.images) == 1 and "artifact_path" not in res.data


def test_observe_scene_alone_attaches_no_image(tool_app, live_on) -> None:
    res = call(tool_app, "live", {"action": "observe", "include": ["scene"]})
    assert res.images == [] and set(res.data) == {"scene"}


# --- inkscape://live/view -----------------------------------------------------


def test_view_resource_returns_the_scene_when_connected(live_on) -> None:
    from inkscape_mcp.live.session import get_session_manager

    get_session_manager().connect()
    scene = LiveScene.model_validate_json(live_view())
    assert scene.selection_count == 1 and scene.canvas.width == 10.0


def test_view_resource_is_a_clean_empty_scene_without_a_session(live_on) -> None:
    scene = LiveScene.model_validate_json(live_view())
    assert scene.selection == [] and scene.visible_objects == []
    assert scene.active_document is None


# --- v4 wire round-trip over the real socket client ---------------------------


def test_get_scene_roundtrips_over_the_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()

    def handler(cmd: str, params: dict) -> dict:
        if cmd == LiveCommand.GET_ACTIVE_DOCUMENT:
            return {"name": "live.svg", "path": "/u/live.svg", "object_count": 1}
        if cmd == LiveCommand.GET_SCENE:
            return {
                "selection": [{"id": "a", "bbox": [0, 0, 4, 4]}],
                "viewport": {"zoom": None, "center": None, "visible_region": None},
                "canvas": {"width": 4.0, "height": 4.0, "units": None},
                "visible_objects": [{"id": "a", "tag": "rect", "label": None, "has_style": True}],
            }
        raise KeyError(cmd)

    secret = "secret-token"
    with mock_helper(token=secret, handler=handler) as port:
        rv = tmp_path / "rv.json"
        rv.write_text(
            json.dumps({"port": port, "token": secret, "protocol_version": PROTOCOL_VERSION}),
            encoding="utf-8",
        )
        rv.chmod(0o600)  # discovery only trusts a private, own-user rendezvous
        monkeypatch.setenv(ENV_RENDEZVOUS, str(rv))
        transport = ExtensionSocketTransport(get_settings())
        transport.connect()
        scene = transport.get_scene()
        transport.disconnect()
    assert scene.active_document is not None and scene.active_document.name == "live.svg"
    assert scene.selection[0].id == "a"
    assert scene.selection[0].bbox == BBox(x=0, y=0, width=4, height=4)
    assert scene.canvas.width == 4.0
    assert isinstance(scene.visible_objects[0], ObjectInfo)
