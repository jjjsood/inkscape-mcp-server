"""Live semantic edit tests: write surface, governance, capability gating, syncability."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_LIVE_ENABLED, ENV_WORKSPACE_ROOTS, Settings, get_settings
from inkscape_mcp.edit.dom import EditError
from inkscape_mcp.live import session as session_mod
from inkscape_mcp.live.edit import (
    build_style,
    build_transform,
    export_live_selection,
    run_live_mutation,
    set_live_viewport,
    validate_region,
    validate_render_scale,
    validate_svg_fragment,
    validate_text,
    validate_viewport,
)
from inkscape_mcp.live.protocol import LiveCommand
from inkscape_mcp.live.records import get_live_operation, list_live_operations
from inkscape_mcp.live.session import LiveSessionManager, get_session_manager, reset_session_manager
from inkscape_mcp.live.sync import sync_live_to_workspace
from inkscape_mcp.live.transport import (
    LiveCapabilityUnsupported,
    LiveMutationResult,
)
from inkscape_mcp.operations import OperationStatus
from inkscape_mcp.registry import Registry
from inkscape_mcp.tools.live import (
    live_apply_to_selection,
    live_export_selection,
    live_insert_svg,
    live_render_view,
    live_set_selected_text,
    live_set_viewport,
)
from inkscape_mcp.workspace.risk import PolicyViolation

from .conftest import FakeTransport

FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKE-CANVAS"


def _connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LiveSessionManager, Settings]:
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    settings = Settings(workspace_roots=[tmp_path], live_enabled=True)
    monkeypatch.setattr(
        session_mod, "select_transport", lambda s, required: FakeTransport(png=FAKE_PNG)
    )
    mgr = LiveSessionManager(settings)
    mgr.connect()
    return mgr, settings


# --- Validated parameter builders (reuse semantics) ----------------------


def test_build_style_validates_and_rejects_injection() -> None:
    style = build_style(fill="red", stroke="#00FF00", stroke_width="2px", opacity=0.5)
    assert style == {"fill": "red", "stroke": "#00ff00", "stroke-width": "2px", "opacity": "0.5"}
    with pytest.raises(EditError):
        build_style(fill="red;stroke:url(#evil)")
    with pytest.raises(EditError):
        build_style(opacity=5.0)


def test_build_transform_composes_and_validates() -> None:
    assert build_transform(dx=3, dy=4) == "translate(3,4)"
    assert build_transform(rotate=90, scale=2) == "rotate(90) scale(2)"
    assert build_transform() is None
    with pytest.raises(EditError):
        build_transform(dx=1)  # translate needs both dx and dy
    with pytest.raises(EditError):
        build_transform(scale=0)  # must be positive


def test_validate_svg_fragment_blocks_unsafe_and_empty() -> None:
    assert validate_svg_fragment('<rect id="x" width="1" height="1"/>').startswith("<rect")
    with pytest.raises(EditError):
        validate_svg_fragment("   ")
    with pytest.raises(EditError):
        validate_svg_fragment("<rect><<<")  # malformed
    with pytest.raises(EditError):
        validate_svg_fragment('<!DOCTYPE x [<!ENTITY a "b">]><rect/>')  # entity/DTD blocked


def test_validate_text_rejects_control_chars() -> None:
    assert validate_text("hello") == "hello"
    with pytest.raises(EditError):
        validate_text("bad\x00null")


# --- Orchestrator governance (+) --------------------------------


def test_run_live_mutation_records_applied_with_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)

    def op(transport):  # type: ignore[no-untyped-def]
        return transport.apply_to_selection(style={"fill": "#ff0000"}, transform=None)

    result = run_live_mutation(
        tool="live_apply_to_selection",
        params={"style": {"fill": "#ff0000"}},
        required_command=LiveCommand.APPLY_TO_SELECTION,
        op=op,
        approval_token="ok",
        manager=mgr,
        settings=settings,
    )
    assert result.affected_ids == ["r1"]
    assert result.undo_friendly is True
    # Before/after canvas renders captured (best-effort) under live artifacts.
    assert result.preview_before and result.preview_before.startswith(".inkscape-mcp/live/")
    assert result.preview_after

    record = get_live_operation(result.operation_id, settings=settings)
    assert record.status is OperationStatus.APPLIED
    assert record.affected_ids == ["r1"]
    assert "before" in record.previews and "after" in record.previews


def test_run_live_mutation_refused_without_approval_does_not_mutate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    before_svg = mgr.require_transport().get_document_svg()

    def op(transport):  # type: ignore[no-untyped-def]
        return transport.apply_to_selection(style={"fill": "#ff0000"}, transform=None)

    with pytest.raises(PolicyViolation):
        run_live_mutation(
            tool="live_apply_to_selection",
            params={},
            required_command=LiveCommand.APPLY_TO_SELECTION,
            op=op,
            approval_token=None,
            manager=mgr,
            settings=settings,
        )
    # Never mutated: the transport SVG is byte-unchanged and no record was applied.
    assert mgr.require_transport().get_document_svg() == before_svg
    assert list_live_operations(settings=settings).count == 0


def test_run_live_mutation_unsupported_command_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    settings = Settings(workspace_roots=[tmp_path], live_enabled=True)

    class ReadOnlyFake(FakeTransport):
        name: ClassVar[str] = "readonly"
        supported_commands: ClassVar[frozenset[LiveCommand]] = frozenset(
            {LiveCommand.GET_SELECTION, LiveCommand.GET_ACTIVE_DOCUMENT, LiveCommand.RENDER_VIEW}
        )

    monkeypatch.setattr(session_mod, "select_transport", lambda s, required: ReadOnlyFake())
    mgr = LiveSessionManager(settings)
    mgr.connect()

    def op(transport):  # type: ignore[no-untyped-def]
        return LiveMutationResult()

    with pytest.raises(LiveCapabilityUnsupported):
        run_live_mutation(
            tool="live_apply_to_selection",
            params={},
            required_command=LiveCommand.APPLY_TO_SELECTION,
            op=op,
            approval_token="ok",
            manager=mgr,
            settings=settings,
        )


def test_live_mutation_is_syncable_to_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)

    def op(transport):  # type: ignore[no-untyped-def]
        return transport.apply_to_selection(style={"fill": "#123456"}, transform=None)

    run_live_mutation(
        tool="live_apply_to_selection",
        params={},
        required_command=LiveCommand.APPLY_TO_SELECTION,
        op=op,
        approval_token="ok",
        manager=mgr,
        settings=settings,
    )
    # The mutated live state syncs to a NEW workspace doc + snapshot reflecting the change.
    reg = Registry(settings)
    dest = tmp_path / "after-edit.svg"
    sync = sync_live_to_workspace(str(dest), manager=mgr, registry=reg, settings=settings)
    assert "#123456" in dest.read_text()
    assert sync.snapshot_id


def test_export_live_selection_writes_png(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    result = export_live_selection(manager=mgr, settings=settings)
    assert result.format == "png"
    assert result.artifact_path.startswith(".inkscape-mcp/live/artifacts/")
    assert (tmp_path / result.artifact_path).read_bytes() == FAKE_PNG
    # Export is read-only feedback — no Operation Record is produced.
    assert list_live_operations(settings=settings).count == 0


# --- View-only validators + engine ----------------------------------


def test_validate_viewport_per_mode() -> None:
    assert validate_viewport("zoom", zoom=2.0) == {"mode": "zoom", "zoom": 2.0}
    assert validate_viewport("zoom", zoom=2.0, center_x=1.0, center_y=3.0) == {
        "mode": "zoom",
        "zoom": 2.0,
        "center": (1.0, 3.0),
    }
    assert validate_viewport("pan", dx=5.0, dy=-2.0) == {"mode": "pan", "dx": 5.0, "dy": -2.0}
    assert validate_viewport("fit_selection") == {"mode": "fit_selection"}
    assert validate_viewport("fit_page") == {"mode": "fit_page"}


def test_validate_viewport_rejects_bad_input() -> None:
    with pytest.raises(EditError):
        validate_viewport("orbit")  # not a fixed mode
    with pytest.raises(EditError):
        validate_viewport("zoom")  # zoom requires a factor
    with pytest.raises(EditError):
        validate_viewport("zoom", zoom=0)  # non-positive zoom out of bounds
    with pytest.raises(EditError):
        validate_viewport("zoom", zoom=float("inf"))  # non-finite
    with pytest.raises(EditError):
        validate_viewport("zoom", zoom=2.0, center_x=1.0)  # center needs both
    with pytest.raises(EditError):
        validate_viewport("pan", dx=1.0)  # pan needs both dx and dy
    with pytest.raises(EditError):
        validate_viewport("pan", dx=1.0, dy=1e12)  # out of bounds


def test_validate_region_and_scale() -> None:
    region = validate_region(0.0, 0.0, 10.0, 5.0)
    assert (region.x, region.y, region.width, region.height) == (0.0, 0.0, 10.0, 5.0)
    with pytest.raises(EditError):
        validate_region(0.0, 0.0, 0.0, 5.0)  # non-positive width
    with pytest.raises(EditError):
        validate_region(0.0, 0.0, 10.0, float("nan"))  # non-finite
    assert validate_render_scale(0.5) == 0.5
    with pytest.raises(EditError):
        validate_render_scale(0)  # out of bounds (must be positive)
    with pytest.raises(EditError):
        validate_render_scale(1e6)  # out of bounds


def test_set_live_viewport_no_operation_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    kwargs = validate_viewport("zoom", zoom=2.0, center_x=1.0, center_y=2.0)
    result = set_live_viewport(kwargs, manager=mgr, settings=settings)
    assert result.mode == "zoom"
    assert result.applied is True
    # View-only — never opens a Live Operation Record.
    assert list_live_operations(settings=settings).count == 0
    # The validated params crossed the transport boundary as typed values.
    transport = mgr.require_transport()
    assert transport.last_viewport == {  # type: ignore[attr-defined]
        "mode": "zoom",
        "zoom": 2.0,
        "center": (1.0, 2.0),
        "dx": None,
        "dy": None,
    }


def test_region_render_targets_a_frame_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from inkscape_mcp.live.render import render_live_view
    from inkscape_mcp.live.transport import RenderRegion

    mgr, settings = _connected(tmp_path, monkeypatch)
    region = RenderRegion(x=0.0, y=0.0, width=4.0, height=4.0)
    result = render_live_view(manager=mgr, settings=settings, region=region, scale=0.5)
    assert result.region is True
    assert result.scale == 0.5
    # The region/scale crossed the boundary and produced a distinct (targeted) frame.
    transport = mgr.require_transport()
    assert transport.last_render == (region, 0.5)  # type: ignore[attr-defined]
    assert (tmp_path / result.artifact_path).read_bytes() == FAKE_PNG + b"-REGION"
    # View-only — no Operation Record.
    assert list_live_operations(settings=settings).count == 0


# --- Tool layer (global singletons) -----------------------------------------


@pytest.fixture
def live_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_LIVE_ENABLED, "1")
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    reset_session_manager()
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    monkeypatch.setattr(
        session_mod, "select_transport", lambda s, required: FakeTransport(png=FAKE_PNG)
    )
    get_session_manager().connect()


def test_tool_apply_requires_approval(live_on: None) -> None:
    with pytest.raises(ToolError):
        live_apply_to_selection(fill="red")  # no approval_token
    # No style or transform supplied is also rejected.
    with pytest.raises(ToolError):
        live_apply_to_selection(approval_token="ok")


def test_tool_apply_with_approval_succeeds(live_on: None) -> None:
    result = live_apply_to_selection(approval_token="ok", fill="red", dx=2, dy=3)
    assert result.affected_ids == ["r1"]
    assert result.operation_id.startswith("op_")


def test_tool_insert_svg_validates_and_applies(live_on: None) -> None:
    with pytest.raises(ToolError):
        live_insert_svg("<rect><<<", approval_token="ok")  # malformed → ToolError
    result = live_insert_svg('<rect id="new1" width="1" height="1"/>', approval_token="ok")
    assert "new1" in result.affected_ids


def test_tool_set_selected_text_rejects_control_chars(live_on: None) -> None:
    with pytest.raises(ToolError):
        live_set_selected_text("bad\x00", approval_token="ok")
    result = live_set_selected_text("hello", approval_token="ok")
    assert result.operation_id.startswith("op_")


def test_tool_export_selection_low_risk_no_approval(live_on: None) -> None:
    result = live_export_selection()
    assert result.format == "png"


# --- View tool layer (low risk, no approval, no Operation Record) ------


def test_tool_set_viewport_modes(live_on: None) -> None:
    from inkscape_mcp.live.records import list_live_operations

    assert live_set_viewport("fit_page").mode == "fit_page"
    assert live_set_viewport("fit_selection").mode == "fit_selection"
    assert live_set_viewport("zoom", zoom=3.0).mode == "zoom"
    assert live_set_viewport("pan", dx=2.0, dy=4.0).mode == "pan"
    # No approval needed and no Operation Record produced (view-only).
    assert list_live_operations(settings=get_settings()).count == 0


def test_tool_set_viewport_rejects_bad_numerics(live_on: None) -> None:
    with pytest.raises(ToolError):
        live_set_viewport("zoom")  # missing factor
    with pytest.raises(ToolError):
        live_set_viewport("zoom", zoom=float("inf"))  # non-finite
    with pytest.raises(ToolError):
        live_set_viewport("pan", dx=1.0)  # pan needs both
    with pytest.raises(ToolError):
        live_set_viewport("nope")  # not a fixed mode


def test_tool_render_view_region_and_scale(live_on: None) -> None:
    result = live_render_view(
        region_x=0.0, region_y=0.0, region_width=4.0, region_height=4.0, scale=0.5
    )
    assert result.region is True
    assert result.scale == 0.5


def test_tool_render_view_partial_region_rejected(live_on: None) -> None:
    with pytest.raises(ToolError):
        live_render_view(region_x=0.0)  # all four region parts required together
    with pytest.raises(ToolError):
        live_render_view(scale=0)  # scale out of bounds


def test_tool_render_view_whole_canvas_still_works(live_on: None) -> None:
    # Backward-compatible: no region/scale renders the whole canvas.
    result = live_render_view()
    assert result.region is False
    assert result.scale is None


def test_tool_render_view_fast_applies_default_downscale(live_on: None) -> None:
    from inkscape_mcp.live.edit import FAST_RENDER_SCALE

    # `fast` with no explicit scale uses the documented loop downscale.
    fast = live_render_view(fast=True)
    assert fast.scale == FAST_RENDER_SCALE
    # An explicit scale always wins over `fast` (full-res / chosen-res on demand).
    explicit = live_render_view(fast=True, scale=2.0)
    assert explicit.scale == 2.0
