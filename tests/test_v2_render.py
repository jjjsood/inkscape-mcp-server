"""`render`: page / object / frame targets, validation, inline image."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.registry import get_registry
from inkscape_mcp.render.cli import RenderResult
from inkscape_mcp.workspace import sandbox
from tests.v2_support import PNG, call, open_doc


def _result(doc_id: str, name: str) -> RenderResult:
    root = Path(get_registry().get(doc_id).root)
    out_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / name
    out.write_bytes(PNG)
    rel = out.relative_to(root).as_posix()
    return RenderResult(
        doc_id=doc_id,
        artifact_path=rel,
        workspace_relative_path=rel,
        format="png",
        width_px=64,
        height_px=32,
        duration_s=0.0,
    )


def test_page_render_returns_an_inline_png_and_small_json(v2_app, v2_ws, v2_renders) -> None:
    doc = open_doc(v2_ws)
    res = call(v2_app, "render", {"doc_id": doc, "width_px": 64})
    assert len(res.images) == 1
    assert res.data["target"] == "page" and res.data["width_px"] == 10
    assert "artifact_path" not in res.data and "workspace_relative_path" not in res.data


def test_object_render_validates_the_id_first(v2_app, v2_ws, monkeypatch) -> None:
    seen: list[str] = []

    def fake_export_object(doc_id, object_id, fmt, **_):
        seen.append(object_id)
        return _result(doc_id, "obj.png")

    monkeypatch.setattr("inkscape_mcp.tools.v2.render.export_object", fake_export_object)
    doc = open_doc(v2_ws)
    res = call(v2_app, "render", {"doc_id": doc, "target": "object", "object_id": "r1"})
    assert seen == ["r1"] and len(res.images) == 1
    with pytest.raises(ToolError, match="object id is not valid"):
        call(v2_app, "render", {"doc_id": doc, "target": "object", "object_id": "a b;rm"})
    with pytest.raises(ToolError, match="object id not found in document"):
        call(v2_app, "render", {"doc_id": doc, "target": "object", "object_id": "ghost"})
    with pytest.raises(ToolError, match="requires object_id"):
        call(v2_app, "render", {"doc_id": doc, "target": "object"})
    assert seen == ["r1"]


def test_frame_render_reports_series_and_index(v2_app, v2_ws, monkeypatch) -> None:
    class Frame(RenderResult):
        series: str
        frame_index: int

    def fake_capture(doc_id, series=None, width_px=None, label=None):
        base = _result(doc_id, "frame-001.png")
        return Frame(**base.model_dump(), series=series or "run", frame_index=1)

    monkeypatch.setattr("inkscape_mcp.tools.v2.render.capture_frame", fake_capture)
    doc = open_doc(v2_ws)
    res = call(v2_app, "render", {"doc_id": doc, "target": "frame", "series": "s1"})
    assert res.data["series"] == "s1" and res.data["frame"] == 1 and len(res.images) == 1


def test_argument_combinations_are_checked(v2_app, v2_ws, v2_renders) -> None:
    doc = open_doc(v2_ws)
    with pytest.raises(ToolError, match="only used with target='object'"):
        call(v2_app, "render", {"doc_id": doc, "object_id": "r1"})
    with pytest.raises(ToolError, match="only used with target='frame'"):
        call(v2_app, "render", {"doc_id": doc, "series": "x"})
    with pytest.raises(ToolError, match="document id not found"):
        call(v2_app, "render", {"doc_id": "nope"})
