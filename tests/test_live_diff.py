"""Focused visual before/after diff tests (E8-04): changed-region bbox, annotated overlay,
artifact-only no-record, record linkage, size-mismatch + host-path-free errors."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from PIL import Image

from inkscape_mcp.config import ENV_LIVE_ENABLED, ENV_WORKSPACE_ROOTS, Settings, get_settings
from inkscape_mcp.live import session as session_mod
from inkscape_mcp.live.diff import (
    LiveDiffError,
    compute_changed_bbox,
    diff_live_operation,
)
from inkscape_mcp.live.records import (
    get_live_operation,
    list_live_operations,
    new_live_operation,
    update_live_operation,
)
from inkscape_mcp.live.session import get_session_manager, reset_session_manager
from inkscape_mcp.live.transport import BBox, SceneSelectionItem
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.risk import RiskClass

from .conftest import FakeTransport


def _settings(tmp_path: Path) -> Settings:
    return Settings(workspace_roots=[tmp_path], live_enabled=True)


def _write_png(path: Path, size: tuple[int, int], fill: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, fill).save(path, format="PNG")


def _png_with_rect(
    path: Path,
    size: tuple[int, int],
    fill: tuple[int, int, int],
    rect: tuple[int, int, int, int],
    rect_fill: tuple[int, int, int],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, fill)
    region = Image.new("RGB", (rect[2] - rect[0], rect[3] - rect[1]), rect_fill)
    img.paste(region, (rect[0], rect[1]))
    img.save(path, format="PNG")


def _record_with_frames(
    tmp_path: Path,
    s: Settings,
    *,
    before_name: str,
    after_name: str,
) -> str:
    """Mint a HIGH live op (approved) and attach before/after preview paths under live artifacts."""
    sandbox.ensure_live_dirs(tmp_path)
    record = new_live_operation(
        tool="live_apply_to_selection",
        risk_class=RiskClass.HIGH,
        params={},
        approval_token="ok",
        settings=s,
    )
    artifacts = sandbox.live_artifacts_dir(tmp_path)
    before_rel = (artifacts / before_name).relative_to(tmp_path).as_posix()
    after_rel = (artifacts / after_name).relative_to(tmp_path).as_posix()
    update_live_operation(
        record,
        settings=s,
        previews={"before": before_rel, "after": after_rel},
    )
    return record.operation_id


# --- Pure pixel-diff -------------------------------------------------------------


def test_compute_changed_bbox_from_known_rect() -> None:
    before = Image.new("RGB", (20, 20), (255, 255, 255))
    after = before.copy()
    after.paste(Image.new("RGB", (5, 4), (0, 0, 0)), (3, 6))  # differing rect at (3,6)-(8,10)
    box = compute_changed_bbox(before, after)
    assert box is not None
    assert (box.x, box.y, box.width, box.height) == (3.0, 6.0, 5.0, 4.0)


def test_compute_changed_bbox_identical_is_none() -> None:
    img = Image.new("RGB", (12, 12), (10, 20, 30))
    assert compute_changed_bbox(img, img.copy()) is None


def test_compute_changed_bbox_size_mismatch_raises() -> None:
    a = Image.new("RGB", (10, 10), (0, 0, 0))
    b = Image.new("RGB", (10, 12), (0, 0, 0))
    with pytest.raises(LiveDiffError):
        compute_changed_bbox(a, b)


# --- Engine: diff_live_operation -------------------------------------------------


def test_diff_live_operation_produces_annotated_artifact(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    artifacts = sandbox.live_artifacts_dir(tmp_path)
    _write_png(artifacts / "before.png", (40, 40), (255, 255, 255))
    _png_with_rect(artifacts / "after.png", (40, 40), (255, 255, 255), (10, 8, 22, 20), (0, 0, 0))
    op_id = _record_with_frames(tmp_path, s, before_name="before.png", after_name="after.png")

    result = diff_live_operation(
        op_id,
        selection=[SceneSelectionItem(id="r1", bbox=BBox(x=5, y=5, width=10, height=10))],
        canvas=BBox(x=0, y=0, width=40, height=40),
        settings=s,
    )
    # Changed-region bbox computed from the two frames.
    assert result.changed_bbox is not None
    assert (result.changed_bbox.x, result.changed_bbox.y) == (10.0, 8.0)
    assert (result.changed_bbox.width, result.changed_bbox.height) == (12.0, 12.0)
    # Annotated overlay artifact landed under the live artifacts dir (server-minted name).
    assert result.artifact_path == f".inkscape-mcp/live/artifacts/live-diff-{op_id}.png"
    out = tmp_path / result.artifact_path
    assert out.is_file()
    assert Image.open(out).size == (40, 40)
    # Selection bbox mapped 1:1 (canvas == image size) and highlighted.
    assert result.highlighted_ids == ["r1"]


def test_diff_links_to_operation_record(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    artifacts = sandbox.live_artifacts_dir(tmp_path)
    _write_png(artifacts / "b.png", (16, 16), (255, 255, 255))
    _png_with_rect(artifacts / "a.png", (16, 16), (255, 255, 255), (2, 2, 6, 6), (0, 0, 0))
    op_id = _record_with_frames(tmp_path, s, before_name="b.png", after_name="a.png")

    result = diff_live_operation(op_id, settings=s)
    assert result.operation_id == op_id
    # The diff artifact path is recorded on the operation (linkable, append-only).
    record = get_live_operation(op_id, settings=s)
    assert result.artifact_path in record.diff_artifacts
    # The source before/after previews are untouched.
    assert set(record.previews) == {"before", "after"}


def test_diff_identical_frames_empty_changed_bbox(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    artifacts = sandbox.live_artifacts_dir(tmp_path)
    _write_png(artifacts / "same1.png", (24, 24), (128, 128, 128))
    _write_png(artifacts / "same2.png", (24, 24), (128, 128, 128))
    op_id = _record_with_frames(tmp_path, s, before_name="same1.png", after_name="same2.png")

    result = diff_live_operation(op_id, settings=s)
    # Identical frames → no changed region (cleanly None), but the overlay artifact still exists.
    assert result.changed_bbox is None
    assert (tmp_path / result.artifact_path).is_file()


def test_diff_size_mismatch_stable_error(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    artifacts = sandbox.live_artifacts_dir(tmp_path)
    _write_png(artifacts / "small.png", (10, 10), (255, 255, 255))
    _write_png(artifacts / "big.png", (10, 20), (255, 255, 255))
    op_id = _record_with_frames(tmp_path, s, before_name="small.png", after_name="big.png")

    with pytest.raises(LiveDiffError) as exc:
        diff_live_operation(op_id, settings=s)
    # Host-path-free, stable message.
    assert str(tmp_path) not in str(exc.value)
    assert "dimension" in str(exc.value)


def test_diff_unknown_operation_id_stable_error(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    sandbox.ensure_live_dirs(tmp_path)
    with pytest.raises(LiveDiffError) as exc:
        diff_live_operation("op_deadbeef", settings=s)
    assert str(tmp_path) not in str(exc.value)


def test_diff_record_without_frames_stable_error(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    sandbox.ensure_live_dirs(tmp_path)
    record = new_live_operation(
        tool="live_apply_to_selection",
        risk_class=RiskClass.HIGH,
        params={},
        approval_token="ok",
        settings=s,
    )
    with pytest.raises(LiveDiffError):
        diff_live_operation(record.operation_id, settings=s)


def test_diff_rejects_frame_outside_artifacts_dir(tmp_path: Path) -> None:
    # A record whose preview path points outside the live artifacts dir is refused (sandbox guard),
    # even though the path is server-side — defense in depth.
    s = _settings(tmp_path)
    sandbox.ensure_live_dirs(tmp_path)
    outside = tmp_path / "evil.png"
    _write_png(outside, (8, 8), (0, 0, 0))
    record = new_live_operation(
        tool="live_apply_to_selection",
        risk_class=RiskClass.HIGH,
        params={},
        approval_token="ok",
        settings=s,
    )
    update_live_operation(
        record,
        settings=s,
        previews={"before": "evil.png", "after": "evil.png"},
    )
    with pytest.raises(LiveDiffError) as exc:
        diff_live_operation(record.operation_id, settings=s)
    assert str(tmp_path) not in str(exc.value)


def test_diff_creates_no_operation_record(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    artifacts = sandbox.live_artifacts_dir(tmp_path)
    _write_png(artifacts / "b2.png", (16, 16), (255, 255, 255))
    _png_with_rect(artifacts / "a2.png", (16, 16), (255, 255, 255), (1, 1, 5, 5), (0, 0, 0))
    op_id = _record_with_frames(tmp_path, s, before_name="b2.png", after_name="a2.png")

    before = list_live_operations(settings=s).count
    diff_live_operation(op_id, settings=s)
    after = list_live_operations(settings=s).count
    # No NEW Operation Record is created by the diff (artifact-only); only the existing op remains.
    assert before == after == 1


# --- Tool layer ------------------------------------------------------------------


@pytest.fixture
def live_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(ENV_LIVE_ENABLED, "1")
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    reset_session_manager()
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    monkeypatch.setattr(session_mod, "select_transport", lambda s, required: FakeTransport())
    get_session_manager().connect()
    return tmp_path


def test_tool_live_diff_view_end_to_end(live_on: Path) -> None:
    from inkscape_mcp.tools.live import live_diff_view

    s = get_settings()
    artifacts = sandbox.live_artifacts_dir(live_on)
    _write_png(artifacts / "tb.png", (32, 32), (255, 255, 255))
    _png_with_rect(artifacts / "ta.png", (32, 32), (255, 255, 255), (4, 4, 16, 16), (0, 0, 0))
    op_id = _record_with_frames(live_on, s, before_name="tb.png", after_name="ta.png")

    result = live_diff_view(op_id)
    assert result.operation_id == op_id
    assert result.changed_bbox is not None
    assert (live_on / result.artifact_path).is_file()


def test_tool_live_diff_view_unknown_op_raises_toolerror(live_on: Path) -> None:
    from inkscape_mcp.tools.live import live_diff_view

    with pytest.raises(ToolError):
        live_diff_view("op_deadbeef")


def test_tool_live_diff_view_registered() -> None:
    import asyncio

    from inkscape_mcp.server import mcp

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "live_diff_view" in names


def test_tool_live_diff_view_size_mismatch_raises_toolerror(live_on: Path) -> None:
    from inkscape_mcp.tools.live import live_diff_view

    s = get_settings()
    artifacts = sandbox.live_artifacts_dir(live_on)
    _write_png(artifacts / "ms.png", (10, 10), (255, 255, 255))
    _write_png(artifacts / "mb.png", (10, 18), (255, 255, 255))
    op_id = _record_with_frames(live_on, s, before_name="ms.png", after_name="mb.png")
    with pytest.raises(ToolError):
        live_diff_view(op_id)
