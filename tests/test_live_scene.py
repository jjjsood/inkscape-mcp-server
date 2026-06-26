"""Structured scene perception tests (E8-02): LiveScene assembly, frame tool, view resource.

Covers: the `LiveScene` is assembled correctly from a faked transport scene payload; the
`live_get_scene` frame tool returns BOTH a PNG path and the scene; the `inkscape://live/view`
resource exposes the scene read-only (empty when no session); the visible-object summary reuses the
headless `ObjectInfo` shape; assembling a scene creates NO Operation Record; and the v4 wire
round-trips `get_scene` over the real socket client.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_LIVE_ENABLED, ENV_WORKSPACE_ROOTS, Settings, get_settings
from inkscape_mcp.document.inspect import ObjectInfo
from inkscape_mcp.live import session as session_mod
from inkscape_mcp.live.protocol import PROTOCOL_VERSION, LiveCommand
from inkscape_mcp.live.records import list_live_operations
from inkscape_mcp.live.scene import get_live_scene, scene_from_result
from inkscape_mcp.live.session import (
    LiveSessionManager,
    get_session_manager,
    reset_session_manager,
)
from inkscape_mcp.live.socket_backend import ENV_RENDEZVOUS, ExtensionSocketTransport
from inkscape_mcp.live.transport import BBox, LiveDocumentRef, LiveScene
from inkscape_mcp.resources.live import live_view
from inkscape_mcp.tools.live import LiveSceneFrame, live_get_scene
from inkscape_mcp.workspace import sandbox

from .conftest import FakeTransport, mock_helper

FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKE-CANVAS"


@pytest.fixture
def live_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live enabled + a FakeTransport connected on the process-wide singleton (tool/resource)."""
    monkeypatch.setenv(ENV_LIVE_ENABLED, "1")
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    reset_session_manager()
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    monkeypatch.setattr(
        session_mod, "select_transport", lambda s, required: FakeTransport(png=FAKE_PNG)
    )
    get_session_manager().connect()


# --- Defensive coercion of a raw get_scene payload --------------------------


def test_scene_from_result_assembles_typed_scene_from_wire_payload() -> None:
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
    assert scene.selection[0].id == "r1"
    assert scene.selection[0].bbox == BBox(x=1.0, y=2.0, width=3.0, height=4.0)
    assert scene.selection[1].bbox is None
    assert scene.viewport.zoom == 2.0
    assert scene.viewport.center == (5.0, 6.0)
    assert scene.viewport.visible_region == BBox(x=0, y=0, width=10, height=10)
    assert scene.canvas.width == 100.0 and scene.canvas.units == "mm"
    assert scene.object_count == 2


def test_scene_from_result_rejects_non_finite_and_malformed_numbers() -> None:
    raw = {
        "selection": "not-a-list",
        "viewport": {"zoom": float("inf"), "center": ["x", "y"]},
        "canvas": {"width": float("nan"), "height": None},
        "visible_objects": [{"id": "r1", "tag": "rect"}, "junk"],
    }
    scene = scene_from_result(raw, None)
    assert scene.selection == []
    assert scene.viewport.zoom is None  # inf rejected
    assert scene.viewport.center is None  # non-numeric rejected
    assert scene.canvas.width is None  # nan rejected
    assert scene.object_count == 1  # the "junk" entry is skipped


def test_visible_object_summary_reuses_headless_objectinfo() -> None:
    scene = scene_from_result(
        {"visible_objects": [{"id": "r1", "tag": "rect", "label": None, "has_style": False}]},
        None,
    )
    # Acceptance criterion: the summary is the SAME ObjectInfo model, not a parallel one.
    assert all(isinstance(o, ObjectInfo) for o in scene.visible_objects)
    assert scene.visible_objects[0] == ObjectInfo(id="r1", tag="rect", label=None, has_style=False)


# --- Orchestrator -----------------------------------------------------------


def test_get_live_scene_pulls_from_connected_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    settings = Settings(workspace_roots=[tmp_path], live_enabled=True)
    monkeypatch.setattr(session_mod, "select_transport", lambda s, required: FakeTransport())
    mgr = LiveSessionManager(settings)
    mgr.connect()

    scene = get_live_scene(manager=mgr)
    assert isinstance(scene, LiveScene)
    assert scene.selection[0].id == "r1"
    assert scene.canvas.width == 10.0
    assert scene.visible_objects[0].tag == "rect"


# --- Frame tool (PNG + scene together) --------------------------------------


def test_live_get_scene_tool_returns_png_path_and_scene(live_on: None, tmp_path: Path) -> None:
    frame = live_get_scene()
    assert isinstance(frame, LiveSceneFrame)
    # Frame carries BOTH a rendered PNG path and the structured scene.
    assert frame.render.artifact_path.endswith(".png")
    assert (tmp_path / frame.render.artifact_path).is_file()
    assert frame.scene.selection_count == 1
    assert frame.scene.visible_objects[0].tag == "rect"
    assert isinstance(frame.scene.visible_objects[0], ObjectInfo)


def test_live_get_scene_creates_no_operation_record(live_on: None, tmp_path: Path) -> None:
    live_get_scene()
    # Read-only perception: nothing is recorded (it never routes through run_live_mutation).
    assert list_live_operations(settings=get_settings()).count == 0
    assert not list(sandbox.live_operations_dir(tmp_path).glob("op_*.json"))


def test_live_get_scene_region_partial_rejected(live_on: None) -> None:
    with pytest.raises(ToolError):
        live_get_scene(region_x=0.0)  # all four region parts required together
    with pytest.raises(ToolError):
        live_get_scene(scale=0)  # scale out of bounds


def test_live_get_scene_errors_cleanly_without_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    reset_session_manager()
    with pytest.raises(ToolError):
        live_get_scene()


# --- Read-only resource -----------------------------------------------------


def test_live_view_resource_returns_scene_when_connected(live_on: None) -> None:
    scene = LiveScene.model_validate_json(live_view())
    assert isinstance(scene, LiveScene)
    assert scene.selection_count == 1
    assert scene.canvas.width == 10.0


def test_live_view_resource_empty_without_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    reset_session_manager()
    scene = LiveScene.model_validate_json(live_view())
    # Clean empty scene, never an error path.
    assert isinstance(scene, LiveScene)
    assert scene.selection == []
    assert scene.visible_objects == []
    assert scene.active_document is None


# --- v4 wire round-trip over the real socket client -------------------------


def test_get_scene_roundtrips_over_socket_v4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    settings = get_settings()

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

    token = "secret-token"
    with mock_helper(token=token, handler=handler) as port:
        rv = tmp_path / "rv.json"
        rv.write_text(
            json.dumps({"port": port, "token": token, "protocol_version": PROTOCOL_VERSION}),
            encoding="utf-8",
        )
        monkeypatch.setenv(ENV_RENDEZVOUS, str(rv))

        transport = ExtensionSocketTransport(settings)
        transport.connect()
        scene = transport.get_scene()
        assert scene.active_document is not None
        assert scene.active_document.name == "live.svg"
        assert scene.selection[0].id == "a"
        assert scene.selection[0].bbox == BBox(x=0, y=0, width=4, height=4)
        assert scene.canvas.width == 4.0
        assert isinstance(scene.visible_objects[0], ObjectInfo)
        transport.disconnect()
