"""Live write path: parameter builders, `run_live_mutation` governance, and `live_edit`.

Engine tests drive `live/edit.py` directly with a `FakeTransport`; tool tests go through
`live_edit(action=apply|insert_svg|set_text, observe=...)` and `live(action=diff)`. `live_edit`
renders before/after internally (for the Operation Record) whatever `observe` asks for.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import ClassVar

import pytest
from fastmcp.exceptions import ToolError
from PIL import Image

from inkscape_mcp.config import ENV_LIVE_ENABLED, ENV_WORKSPACE_ROOTS, Settings, get_settings
from inkscape_mcp.edit.dom import EditError
from inkscape_mcp.live import session as session_mod
from inkscape_mcp.live.edit import (
    build_style,
    build_transform,
    export_live_selection,
    run_live_mutation,
    validate_svg_fragment,
    validate_text,
)
from inkscape_mcp.live.protocol import LiveCommand
from inkscape_mcp.live.records import get_live_operation, list_live_operations
from inkscape_mcp.live.session import LiveSessionManager, get_session_manager, reset_session_manager
from inkscape_mcp.live.transport import (
    LiveCapabilityUnsupported,
    LiveConnectionError,
    LiveMutationResult,
)
from inkscape_mcp.operations import OperationStatus
from inkscape_mcp.workspace.risk import PolicyViolation
from tests.conftest import FakeTransport
from tests.tool_support import call

FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKE-CANVAS"


def _png(color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, format="PNG")
    return buf.getvalue()


class PngTransport(FakeTransport):
    """Emits decodable, CHANGING frames (white, then red) so a before/after diff has pixels."""

    def __init__(self) -> None:
        super().__init__()
        self._renders = 0

    def render_view(self, region=None, scale=None):  # type: ignore[no-untyped-def]
        self._renders += 1
        return _png((255, 255, 255) if self._renders <= 1 else (255, 0, 0))

    def apply_to_selection(self, **kwargs):  # type: ignore[no-untyped-def]
        self.state_revision = "rev-edited"  # a new revision keeps the render cache from reusing
        return super().apply_to_selection(**kwargs)


class ReadOnlyTransport(FakeTransport):
    name: ClassVar[str] = "readonly"
    supported_commands: ClassVar[frozenset[LiveCommand]] = frozenset(
        {LiveCommand.GET_SELECTION, LiveCommand.GET_ACTIVE_DOCUMENT, LiveCommand.RENDER_VIEW}
    )


def _serve(monkeypatch: pytest.MonkeyPatch, transport: FakeTransport) -> None:
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    monkeypatch.setattr(session_mod, "select_transport", lambda s, required: transport)


def _connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: FakeTransport | None = None
) -> tuple[LiveSessionManager, Settings]:
    """A standalone manager + settings (engine level, no globals)."""
    _serve(monkeypatch, transport or FakeTransport(png=FAKE_PNG))
    settings = Settings(workspace_roots=[tmp_path], live_enabled=True)
    mgr = LiveSessionManager(settings)
    mgr.connect()
    return mgr, settings


def _apply(**style: str):  # type: ignore[no-untyped-def]
    return lambda t: t.apply_to_selection(style=style, transform=None)


def _mutate(mgr, settings, approval_token="ok", op=None, command=LiveCommand.APPLY_TO_SELECTION):  # type: ignore[no-untyped-def]
    return run_live_mutation(
        tool="live_edit.apply",
        params={},
        required_command=command,
        op=op or _apply(fill="#ff0000"),
        approval_token=approval_token,
        manager=mgr,
        settings=settings,
    )


# --- validated parameter builders ---------------------------------------------


def test_build_style_normalises_and_rejects_injection() -> None:
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
        build_transform(scale=0)


def test_validate_svg_fragment_blocks_unsafe_and_empty() -> None:
    assert validate_svg_fragment('<rect id="x" width="1" height="1"/>').startswith("<rect")
    for bad in ("   ", "<rect><<<", '<!DOCTYPE x [<!ENTITY a "b">]><rect/>'):
        with pytest.raises(EditError):
            validate_svg_fragment(bad)


def test_validate_text_rejects_control_chars() -> None:
    assert validate_text("hello") == "hello"
    with pytest.raises(EditError):
        validate_text("bad\x00null")


# --- run_live_mutation: governance --------------------------------------------


def test_mutation_records_applied_with_before_and_after_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    result = _mutate(mgr, settings)
    assert result.affected_ids == ["r1"] and result.undo_friendly is True
    assert result.preview_before and result.preview_before.startswith(".inkscape-mcp/live/")
    assert result.preview_after
    record = get_live_operation(result.operation_id, settings=settings)
    assert record.status is OperationStatus.APPLIED
    assert record.affected_ids == ["r1"]
    assert set(record.previews) == {"before", "after"}


def test_mutation_without_approval_is_refused_and_does_not_mutate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    before = mgr.require_transport().get_document_svg()
    with pytest.raises(PolicyViolation):
        _mutate(mgr, settings, approval_token=None)
    assert mgr.require_transport().get_document_svg() == before
    assert list_live_operations(settings=settings).count == 0


def test_mutation_unsupported_by_transport_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch, ReadOnlyTransport())
    with pytest.raises(LiveCapabilityUnsupported):
        _mutate(mgr, settings, op=lambda t: LiveMutationResult())
    assert list_live_operations(settings=settings).count == 0


def test_failed_mutation_is_discarded_not_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)

    def boom(_t):  # type: ignore[no-untyped-def]
        raise LiveConnectionError("link dropped")

    with pytest.raises(LiveConnectionError):
        _mutate(mgr, settings, op=boom)
    log = list_live_operations(settings=settings)
    assert [r.status for r in log.operations] == [OperationStatus.DISCARDED]


def test_export_live_selection_writes_png_without_a_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings = _connected(tmp_path, monkeypatch)
    result = export_live_selection(manager=mgr, settings=settings)
    assert result.format == "png"
    assert result.artifact_path.startswith(".inkscape-mcp/live/artifacts/")
    assert (tmp_path / result.artifact_path).read_bytes() == FAKE_PNG
    assert list_live_operations(settings=settings).count == 0


# --- live_edit tool -----------------------------------------------------------


@pytest.fixture
def live(tool_ws, monkeypatch: pytest.MonkeyPatch) -> PngTransport:
    """Live on, one PngTransport handed out by connect (the process-wide manager)."""
    monkeypatch.setenv(ENV_LIVE_ENABLED, "1")
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tool_ws))
    get_settings.cache_clear()
    reset_session_manager()
    transport = PngTransport()
    _serve(monkeypatch, transport)
    yield transport
    reset_session_manager()


def _edit(app, **args):  # type: ignore[no-untyped-def]
    return call(app, "live_edit", {"approval_token": "ok", **args})


def _records():  # type: ignore[no-untyped-def]
    return list_live_operations(settings=get_settings()).operations


@pytest.mark.parametrize(
    "args",
    [
        {"action": "apply", "style": {"fill": "red;stroke:url(#evil)"}},
        {"action": "apply", "style": {"opacity": 5.0}},
        {"action": "apply", "transform": {"dx": 1}},
        {"action": "apply", "transform": {"scale": 0}},
        {"action": "insert_svg", "svg_fragment": "<rect><<<"},
        {"action": "insert_svg", "svg_fragment": '<!DOCTYPE x [<!ENTITY a "b">]><rect/>'},
        {"action": "set_text", "text": "bad\x00null"},
    ],
    ids=["fill-injection", "opacity", "dx-alone", "scale-0", "malformed", "doctype", "ctrl-char"],
)
def test_live_edit_rejects_invalid_values_before_touching_the_canvas(
    tool_app, live, args: dict
) -> None:
    before = live.get_document_svg()
    with pytest.raises(ToolError):
        _edit(tool_app, **args)
    assert live.get_document_svg() == before
    assert _records() == []


def test_live_edit_apply_composes_style_and_transform_into_one_record(tool_app, live) -> None:
    res = _edit(
        tool_app,
        action="apply",
        style={"fill": "red"},
        transform={"rotate": 90, "scale": 2},
    )
    assert res.data["affected_ids"] == ["r1"] and res.data["operation_id"].startswith("op_")
    assert res.images == []  # observe defaults to none
    assert 'transform="rotate(90) scale(2)"' in live.get_document_svg()
    (record,) = _records()
    assert record.tool == "live_edit.apply" and record.status is OperationStatus.APPLIED
    # before/after renders are taken for the record even though nothing was asked to be returned
    assert set(record.previews) == {"before", "after"}


def test_live_edit_without_approval_is_refused_and_leaves_no_record(tool_app, live) -> None:
    before = live.get_document_svg()
    with pytest.raises(ToolError, match="approval"):
        call(tool_app, "live_edit", {"action": "apply", "style": {"fill": "#f00"}})
    assert live.get_document_svg() == before
    assert _records() == []


def test_live_edit_insert_svg_and_set_text_report_their_targets(tool_app, live) -> None:
    ins = _edit(
        tool_app, action="insert_svg", svg_fragment='<rect id="new1" width="1" height="1"/>'
    )
    assert "new1" in ins.data["affected_ids"]
    assert 'id="new1"' in live.get_document_svg()
    txt = _edit(tool_app, action="set_text", text="hello")
    assert txt.data["affected_ids"] == ["r1"]
    assert [r.tool for r in _records()] == ["live_edit.set_text", "live_edit.insert_svg"]


def test_live_edit_observe_diff_links_an_overlay_to_the_record(tool_app, live) -> None:
    res = _edit(tool_app, action="apply", style={"fill": "#00ff00"}, observe="diff")
    assert len(res.images) == 1
    assert res.data["changed_bbox"] and "artifact_path" not in res.data
    (record,) = _records()
    assert len(record.diff_artifacts) == 1


def test_live_edit_observe_render_failure_is_a_warning_not_an_error(tool_app, live) -> None:
    def broken(region=None, scale=None):  # type: ignore[no-untyped-def]
        raise LiveConnectionError("render failed")

    live.render_view = broken  # type: ignore[method-assign]
    res = _edit(tool_app, action="apply", style={"fill": "#00f"}, observe="render")
    assert res.images == [] and res.data["warning"] == "render unavailable"
    assert res.data["operation_id"]  # the mutation itself went through
    assert [r.status for r in _records()] == [OperationStatus.APPLIED]


def test_live_edit_on_a_read_only_transport_names_the_probe(
    tool_app, live, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, ReadOnlyTransport())
    with pytest.raises(ToolError, match=r"live\(action='check'\)"):
        _edit(tool_app, action="apply", style={"fill": "#f00"})
    assert _records() == []


def test_live_edit_when_live_is_disabled_says_how_to_enable(
    tool_app, live, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_LIVE_ENABLED, "0")
    get_settings.cache_clear()
    reset_session_manager()
    with pytest.raises(ToolError, match="live mode is disabled"):
        _edit(tool_app, action="apply", style={"fill": "#f00"})


def test_live_edit_result_is_diffable_and_syncable(tool_app, live, tool_ws) -> None:
    op_id = _edit(tool_app, action="apply", style={"fill": "#123456"}).data["operation_id"]
    # `live(diff)` reads the Operation Record only: image attached, server path stripped
    diff = call(tool_app, "live", {"action": "diff", "operation_id": op_id})
    assert len(diff.images) == 1 and "artifact_path" not in diff.data
    assert len(_records()) == 1  # a diff is not a mutation
    with pytest.raises(ToolError, match="no live operation"):
        call(tool_app, "live", {"action": "diff", "operation_id": "op_missing"})
    # the edited canvas can be pulled into the workspace
    saved = call(tool_app, "open", {"from_live": {"dest_path": "edited.svg"}})
    assert saved.data["saved_path"].endswith("edited.svg")
    assert "#123456" in (tool_ws / "edited.svg").read_text()
    assert get_session_manager().status().connected
