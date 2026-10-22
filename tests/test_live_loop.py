"""Live view loop: one perceive -> act -> observe iteration with zero new authority.

`live/loop.py::run_session_step` is tested directly (the `live_session_step` tool is gone); the
same iteration through the public tools is `live(observe)` then `live_edit(observe='diff')`.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image

from inkscape_mcp.config import ENV_LIVE_ENABLED, ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.edit.dom import EditError
from inkscape_mcp.live import session as session_mod
from inkscape_mcp.live.loop import LiveSessionStepResult, StepAction, run_session_step
from inkscape_mcp.live.records import list_live_operations
from inkscape_mcp.live.session import get_session_manager, reset_session_manager
from inkscape_mcp.workspace.risk import PolicyViolation
from tests.conftest import FakeTransport
from tests.tool_support import call


def _png(color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, format="PNG")
    return buf.getvalue()


class PngTransport(FakeTransport):
    """Decodable frames that CHANGE after the first render, so the observe diff has pixels."""

    def __init__(self) -> None:
        super().__init__()
        self._renders = 0

    def render_view(self, region=None, scale=None):  # type: ignore[no-untyped-def]
        self._renders += 1
        return _png((255, 255, 255) if self._renders <= 1 else (255, 0, 0))

    def apply_to_selection(self, **kwargs):  # type: ignore[no-untyped-def]
        self.state_revision = "rev-edited"  # keeps the render cache from reusing the old frame
        return super().apply_to_selection(**kwargs)


@pytest.fixture
def live_on(tool_ws, monkeypatch: pytest.MonkeyPatch):
    """Live on with a connected PngTransport on the process-wide manager; returns the root."""
    monkeypatch.setenv(ENV_LIVE_ENABLED, "1")
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tool_ws))
    get_settings.cache_clear()
    reset_session_manager()
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    monkeypatch.setattr(session_mod, "select_transport", lambda s, required: PngTransport())
    get_session_manager().connect()
    yield tool_ws
    reset_session_manager()


def _count() -> int:
    return list_live_operations(settings=get_settings()).count


# --- perceive-only ------------------------------------------------------------


def test_perceive_only_step_returns_scene_and_frame_and_records_nothing(live_on) -> None:
    result = run_session_step()
    assert isinstance(result, LiveSessionStepResult)
    assert result.acted is False and result.action is None
    assert result.before_scene.selection_count == 1
    assert (live_on / result.before_frame.artifact_path).is_file()
    assert result.operation_id is None and result.edit is None and result.diff is None
    assert result.after_scene is None
    assert _count() == 0


# --- act: HIGH gate -----------------------------------------------------------


def test_act_without_approval_is_refused_before_any_mutation(live_on) -> None:
    transport = get_session_manager().require_transport()
    before = transport.get_document_svg()
    with pytest.raises(PolicyViolation):
        run_session_step(action=StepAction.APPLY, fill="red", approval_token=None)
    assert transport.get_document_svg() == before
    assert _count() == 0


# --- act + observe ------------------------------------------------------------


def test_apply_step_records_one_operation_and_links_a_focused_diff(live_on) -> None:
    result = run_session_step(action=StepAction.APPLY, approval_token="ok", fill="red", dx=2, dy=3)
    assert result.acted is True and result.action is StepAction.APPLY
    assert result.operation_id and result.operation_id.startswith("op_")
    assert result.edit is not None and result.edit.affected_ids == ["r1"]
    log = list_live_operations(settings=get_settings())
    assert log.count == 1 and log.operations[0].operation_id == result.operation_id
    assert result.diff is not None and result.diff.operation_id == result.operation_id
    assert (live_on / result.diff.artifact_path).is_file()
    assert result.diff.artifact_path in log.operations[0].diff_artifacts
    assert result.after_scene is not None and result.after_frame is not None


@pytest.mark.parametrize(
    ("kwargs", "affected"),
    [
        (
            {
                "action": StepAction.INSERT_SVG,
                "svg_fragment": '<rect id="new1" width="1" height="1"/>',
            },
            "new1",
        ),
        ({"action": StepAction.SET_TEXT, "text": "hello"}, "r1"),
    ],
    ids=["insert_svg", "set_text"],
)
def test_other_acts_go_through_the_governed_path(live_on, kwargs, affected: str) -> None:
    result = run_session_step(approval_token="ok", **kwargs)
    assert result.acted is True and result.operation_id
    assert result.edit is not None and affected in result.edit.affected_ids
    assert _count() == 1


# --- fixed semantic op set ----------------------------------------------------


@pytest.mark.parametrize("raw", ["run_action", "exec_code"])
def test_the_act_set_is_a_closed_enum_with_no_raw_action_or_code(raw: str) -> None:
    assert {a.value for a in StepAction} == {"apply", "insert_svg", "set_text"}
    with pytest.raises(ValueError):
        StepAction(raw)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": StepAction.APPLY},  # needs a style or transform
        {"action": StepAction.APPLY, "dx": 1},  # translate needs dx and dy
        {"action": StepAction.INSERT_SVG},
        {"action": StepAction.INSERT_SVG, "svg_fragment": "<rect><<<"},
        {"action": StepAction.SET_TEXT},
        {"action": StepAction.SET_TEXT, "text": "bad\x00"},
        {"action": StepAction.APPLY, "fill": "red;stroke:url(#evil)"},
    ],
    ids=[
        "apply-empty",
        "dx-alone",
        "no-fragment",
        "malformed",
        "no-text",
        "ctrl-char",
        "injection",
    ],
)
def test_invalid_act_params_are_rejected_before_any_record(live_on, kwargs) -> None:
    with pytest.raises(EditError):
        run_session_step(approval_token="ok", **kwargs)
    assert _count() == 0


def test_the_step_mutation_path_is_the_write_path(live_on, monkeypatch) -> None:
    import inkscape_mcp.live.loop as loop_mod

    seen: dict[str, object] = {}
    real = loop_mod.run_live_mutation

    def spy(**kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(loop_mod, "run_live_mutation", spy)
    run_session_step(action=StepAction.APPLY, approval_token="ok", fill="blue")
    assert seen["approval_token"] == "ok" and seen["tool"] == "live_session_step:apply"


# --- the same iteration through the public tools ------------------------------


def test_one_iteration_through_live_and_live_edit(tool_app, live_on) -> None:
    perceived = call(tool_app, "live", {"action": "observe", "include": ["scene", "view"]})
    assert perceived.data["scene"]["selection_count"] == 1 and len(perceived.images) == 1
    assert _count() == 0  # perceiving records nothing

    acted = call(
        tool_app,
        "live_edit",
        {"action": "apply", "style": {"fill": "red"}, "observe": "diff", "approval_token": "ok"},
    )
    assert acted.data["affected_ids"] == ["r1"] and len(acted.images) == 1
    (record,) = list_live_operations(settings=get_settings()).operations
    assert record.operation_id == acted.data["operation_id"] and record.diff_artifacts

    observed = call(tool_app, "live", {"action": "observe", "include": ["scene"]})
    assert observed.data["scene"]["selection_count"] == 1


# --- live_canvas_assist prompt ------------------------------------------------


def test_live_canvas_assist_prompt_is_registered_and_carries_the_goal() -> None:
    from inkscape_mcp.prompts.live import live_canvas_assist
    from inkscape_mcp.server import mcp

    assert "live_canvas_assist" in {p.name for p in asyncio.run(mcp.list_prompts())}
    body = live_canvas_assist(goal="make the logo blue")
    assert "make the logo blue" in body and "approval_token" in body
    assert "perceive" in body.lower()
    for op in ("apply", "insert_svg", "set_text"):
        assert op in body
