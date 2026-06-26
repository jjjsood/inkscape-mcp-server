"""Live view loop orchestrator tests: perceive→decide→act→observe, zero new authority.

Covers: a perceive-only step returns scene/frame and creates NO Operation Record; an act routes
through `run_live_mutation` and is REFUSED without `approval_token` (HIGH gate); a successful act
produces a Live Operation Record + links a focused diff artifact; only the fixed semantic ops are
accepted (an unknown op is rejected at the type boundary); the `live_canvas_assist` Prompt is
registered (smoke); and the step's mutation path IS the write path (no new authority).
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from PIL import Image

from inkscape_mcp.config import ENV_LIVE_ENABLED, ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.live import session as session_mod
from inkscape_mcp.live.loop import LiveSessionStepResult, StepAction, run_session_step
from inkscape_mcp.live.records import list_live_operations
from inkscape_mcp.live.session import get_session_manager, reset_session_manager
from inkscape_mcp.prompts import live as live_prompts  # noqa: F401  (self-registers the prompt)
from inkscape_mcp.server import mcp
from inkscape_mcp.tools.live import live_session_step

from .conftest import FakeTransport


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    """A real, decodable PNG so the observe-phase diff can actually open before/after frames."""
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, format="PNG")
    return buf.getvalue()


class PngTransport(FakeTransport):
    """FakeTransport emitting valid, CHANGING PNG frames so the loop diff lands a real artifact."""

    def __init__(self) -> None:
        super().__init__()
        self._renders = 0

    def render_view(self, region=None, scale=None):  # type: ignore[no-untyped-def]
        # First render (before) is white; later renders (after) are red — a real pixel delta.
        self._renders += 1
        return _png_bytes((255, 255, 255) if self._renders <= 1 else (255, 0, 0))


@pytest.fixture
def live_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(ENV_LIVE_ENABLED, "1")
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    reset_session_manager()
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    monkeypatch.setattr(session_mod, "select_transport", lambda s, required: PngTransport())
    get_session_manager().connect()
    return tmp_path


# --- Perceive-only ---------------------------------------------------------------


def test_perceive_only_step_returns_scene_and_frame_no_record(live_on: Path) -> None:
    s = get_settings()
    result = run_session_step()  # no action ⇒ perceive-only
    assert isinstance(result, LiveSessionStepResult)
    assert result.acted is False
    assert result.action is None
    assert result.before_scene.selection_count == 1
    assert (live_on / result.before_frame.artifact_path).is_file()
    # Read-only perception: nothing recorded, nothing observed.
    assert result.operation_id is None
    assert result.edit is None
    assert result.diff is None
    assert result.after_scene is None
    assert list_live_operations(settings=s).count == 0


def test_perceive_only_tool_no_approval_required(live_on: Path) -> None:
    # The tool wrapper: perceive-only needs no approval_token and never raises the HIGH gate.
    result = live_session_step()
    assert result.acted is False


# --- Act: HIGH gate (refused without approval) -----------------------------------


def test_act_refused_without_approval_token(live_on: Path) -> None:
    s = get_settings()
    with pytest.raises(ToolError):
        live_session_step(action=StepAction.APPLY, fill="red")  # no approval_token
    # The gate fires BEFORE mutation — never mutate unapproved (X1). No record persisted.
    assert list_live_operations(settings=s).count == 0


def test_act_engine_refused_without_approval_token(live_on: Path) -> None:
    from inkscape_mcp.workspace.risk import PolicyViolation

    with pytest.raises(PolicyViolation):
        run_session_step(action=StepAction.APPLY, fill="red", approval_token=None)


# --- Act: success → record + linked diff (observe) -------------------------------


def test_act_apply_produces_record_and_links_diff(live_on: Path) -> None:
    s = get_settings()
    result = live_session_step(action=StepAction.APPLY, approval_token="ok", fill="red", dx=2, dy=3)
    assert result.acted is True
    assert result.action is StepAction.APPLY
    assert result.operation_id is not None and result.operation_id.startswith("op_")
    assert result.edit is not None and result.edit.affected_ids == ["r1"]
    # Exactly one Live Operation Record from the act (the governed path).
    log = list_live_operations(settings=s)
    assert log.count == 1
    assert log.operations[0].operation_id == result.operation_id
    # OBSERVE: a focused diff was produced + linked back to the record's diff_artifacts.
    assert result.diff is not None
    assert result.diff.operation_id == result.operation_id
    assert (live_on / result.diff.artifact_path).is_file()
    assert result.diff.artifact_path in log.operations[0].diff_artifacts
    # After-scene/frame captured for the observe phase.
    assert result.after_scene is not None
    assert result.after_frame is not None


def test_act_insert_svg_routes_through_governed_path(live_on: Path) -> None:
    result = live_session_step(
        action=StepAction.INSERT_SVG,
        approval_token="ok",
        svg_fragment='<rect id="new1" width="1" height="1"/>',
    )
    assert result.acted is True
    assert result.operation_id is not None
    assert "new1" in (result.edit.affected_ids if result.edit else [])


def test_act_set_text_routes_through_governed_path(live_on: Path) -> None:
    result = live_session_step(action=StepAction.SET_TEXT, approval_token="ok", text="hello")
    assert result.acted is True
    assert result.operation_id is not None


# --- Fixed semantic op set (no raw action/code) ----------------------------------


def test_unknown_op_rejected(live_on: Path) -> None:
    # A string outside the fixed StepAction enum is rejected (no raw passthrough).
    with pytest.raises(ToolError):
        live_session_step(action="run_action", approval_token="ok")  # type: ignore[arg-type]
    with pytest.raises(ToolError):
        live_session_step(action="exec_code", approval_token="ok")  # type: ignore[arg-type]


def test_apply_requires_a_style_or_transform_param(live_on: Path) -> None:
    # An `apply` with no style/transform is rejected (mirrors live_apply_to_selection).
    with pytest.raises(ToolError):
        live_session_step(action=StepAction.APPLY, approval_token="ok")


def test_insert_svg_requires_a_fragment(live_on: Path) -> None:
    with pytest.raises(ToolError):
        live_session_step(action=StepAction.INSERT_SVG, approval_token="ok")  # no svg_fragment


def test_set_text_requires_text(live_on: Path) -> None:
    with pytest.raises(ToolError):
        live_session_step(action=StepAction.SET_TEXT, approval_token="ok")  # no text


def test_insert_svg_rejects_unsafe_fragment(live_on: Path) -> None:
    with pytest.raises(ToolError):
        live_session_step(
            action=StepAction.INSERT_SVG, approval_token="ok", svg_fragment="<rect><<<"
        )


# --- No new authority: the step's mutation path == the write path --------------


def test_step_mutation_path_is_the_e4_write_path(
    live_on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The act must flow through run_live_mutation — not a new mutation path.
    import inkscape_mcp.live.loop as loop_mod

    seen: dict[str, object] = {}
    real = loop_mod.run_live_mutation

    def spy(**kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(loop_mod, "run_live_mutation", spy)
    live_session_step(action=StepAction.APPLY, approval_token="ok", fill="blue")
    assert seen.get("approval_token") == "ok"
    assert seen.get("tool") == "live_session_step:apply"


# --- Registration smoke ----------------------------------------------------------


def test_live_session_step_tool_registered() -> None:
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "live_session_step" in names


def test_live_canvas_assist_prompt_registered() -> None:
    names = {p.name for p in asyncio.run(mcp.list_prompts())}
    assert "live_canvas_assist" in names


def test_live_canvas_assist_prompt_content() -> None:
    from inkscape_mcp.prompts.live import live_canvas_assist

    body = live_canvas_assist(goal="make the logo blue")
    assert "make the logo blue" in body
    assert "live_session_step" in body
    assert "approval_token" in body
    # Orients on the bounded loop + the fixed semantic op set.
    assert "perceive" in body.lower()
    for op in ("apply", "insert_svg", "set_text"):
        assert op in body
