"""`export`: document sets, dry-run profile projection, the shared byte budget, invalid targets.

The Inkscape export engine is faked (`export_document` / `export_object` write stub files), so the
tests exercise the tool's own logic. Per-kind coverage lives in `test_export_files.py`,
`test_export_profiles.py`, `test_export_batch.py` and `test_save_as.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.registry import get_registry
from inkscape_mcp.render.cli import RenderResult
from inkscape_mcp.workspace import sandbox
from tests.tool_support import PNG, call, open_doc


@pytest.fixture
def engine(monkeypatch, tool_ws):
    """Fake the export engine; returns the list of (doc_id, object_id, format, width, prefix)."""
    calls: list[tuple] = []

    def make(doc_id, object_id, fmt, width_px, name_prefix, **_):
        calls.append((doc_id, object_id, fmt, width_px, name_prefix))
        root = Path(get_registry().get(doc_id).root)
        out_dir = sandbox.artifacts_dir(root, doc_id) / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{name_prefix or 'export'}-{len(calls)}"
        out = out_dir / f"{stem}.{fmt}"
        out.write_bytes(PNG if fmt == "png" else b"<svg/>")
        rel = out.relative_to(root).as_posix()
        return RenderResult(
            doc_id=doc_id,
            workspace_relative_path=rel,
            format=fmt,
            width_px=width_px if fmt == "png" else None,
            height_px=width_px if fmt == "png" else None,
            duration_s=0.0,
        )

    def fake_doc(doc_id, fmt, width_px=None, out_dir=None, name_prefix=None, **_):
        return make(doc_id, None, fmt, width_px, name_prefix)

    def fake_obj(doc_id, object_id, fmt="png", width_px=None, out_dir=None, name_prefix=None, **_):
        return make(doc_id, object_id, fmt, width_px, name_prefix)

    for module in ("inkscape_mcp.render.batch", "inkscape_mcp.render.profiles"):
        monkeypatch.setattr(f"{module}.export_document", fake_doc)
    monkeypatch.setattr("inkscape_mcp.render.batch.export_object", fake_obj)
    return calls


def _export(app, doc, targets, **kw):
    return call(app, "export", {"doc_id": doc, "targets": targets, **kw}).data


def test_a_set_prefixes_files_with_each_doc_id(tool_app, tool_ws, engine) -> None:
    a, b = open_doc(tool_ws, "a.svg"), open_doc(tool_ws, "b.svg")
    data = _export(tool_app, [a, b], [{"kind": "file", "format": "png"}])
    assert data["count"] == 2
    assert {call_[4] for call_ in engine} == {a, b}
    assert {x["doc_id"] for x in data["artifacts"]} == {a, b}


def test_a_set_validates_every_id_before_exporting(tool_app, tool_ws, engine) -> None:
    a = open_doc(tool_ws)
    with pytest.raises(ToolError, match="document id not found"):
        _export(tool_app, [a, "nope"], [{"kind": "file", "format": "png"}])
    assert engine == []


def test_save_as_is_rejected_for_a_set(tool_app, tool_ws) -> None:
    a, b = open_doc(tool_ws, "a.svg"), open_doc(tool_ws, "b.svg")
    with pytest.raises(ToolError, match="save_as writes one document"):
        _export(tool_app, [a, b], [{"kind": "save_as", "dest_path": "x.svg"}])


@pytest.mark.parametrize(
    ("profile", "formats"),
    [
        ({"name": "web", "widths": [256, 512]}, ["png", "png", "svg"]),
        ({"name": "icons", "sizes": [16, 32]}, ["png", "png"]),
        ({"name": "print"}, ["pdf"]),
    ],
)
def test_dry_run_projects_profile_sizes(tool_app, tool_ws, engine, profile, formats) -> None:
    doc = open_doc(tool_ws)
    data = _export(tool_app, doc, [{"kind": "profile", **profile}], dry_run=True)
    assert [p["format"] for p in data["plan"]] == formats
    assert all(p["profile"] == profile["name"] and p["projected_bytes"] > 0 for p in data["plan"])
    assert data["projected_bytes"] == sum(p["projected_bytes"] for p in data["plan"])
    assert engine == []


def test_dry_run_rejects_what_the_real_run_would(tool_app, tool_ws, engine) -> None:
    doc = open_doc(tool_ws)
    with pytest.raises(ToolError, match="icon size"):
        _export(tool_app, doc, [{"kind": "profile", "name": "icons", "sizes": [0]}], dry_run=True)
    assert engine == []


def test_budget_refuses_a_real_profile_export_before_writing(tool_app, tool_ws, engine) -> None:
    doc = open_doc(tool_ws)
    with pytest.raises(ToolError, match="byte budget"):
        _export(tool_app, doc, [{"kind": "profile", "name": "icons"}], byte_budget=10)
    assert engine == []
    assert list(tool_ws.rglob("*.png")) == []


def test_file_and_profile_targets_share_one_budget(tool_app, tool_ws, engine) -> None:
    doc = open_doc(tool_ws)
    png = {"kind": "file", "format": "png", "width_px": 128}
    pdf = {"kind": "profile", "name": "print"}
    alone = [_export(tool_app, doc, [t], dry_run=True)["projected_bytes"] for t in (png, pdf)]
    budget = max(alone)  # each target fits on its own, both together do not
    planned = _export(tool_app, doc, [png, pdf], dry_run=True, byte_budget=budget)
    assert planned["within_budget"] is False
    with pytest.raises(ToolError, match="byte budget"):
        _export(tool_app, doc, [png, pdf], byte_budget=budget)
    assert engine == []


def test_invalid_targets(tool_app, tool_ws, engine) -> None:
    doc = open_doc(tool_ws)
    with pytest.raises(ToolError, match="at least one target"):
        _export(tool_app, doc, [])
    with pytest.raises(ToolError):
        _export(tool_app, doc, [{"kind": "file", "format": "gif"}])
    with pytest.raises(ToolError):
        _export(tool_app, doc, [{"kind": "file", "format": "png", "object_id": "ghost"}])
    with pytest.raises(ToolError):
        _export(tool_app, doc, [{"kind": "nonsense"}])
