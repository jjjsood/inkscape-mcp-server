"""`export`: file / profile / save_as targets, sets, dry run, budget, error context.

The Inkscape export engine is faked (`export_document` / `export_object` write stub files), so the
tests exercise the tool's own logic: target routing, prefixes, aggregation and failure context.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.registry import get_registry
from inkscape_mcp.render.cli import RenderError, RenderResult
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
            artifact_path=rel,
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


def test_png_and_svg_in_one_call(tool_app, tool_ws, engine) -> None:
    doc = open_doc(tool_ws)
    data = _export(
        tool_app,
        doc,
        [
            {"kind": "file", "format": "png", "width_px": 256},
            {"kind": "file", "format": "svg"},
        ],
    )
    assert data["count"] == 2
    assert [a["format"] for a in data["artifacts"]] == ["png", "svg"]
    assert data["artifacts"][0]["width_px"] == 256
    assert data["total_bytes"] == sum(a["bytes"] for a in data["artifacts"]) > 0
    assert all(a["path"].endswith(a["format"]) for a in data["artifacts"])


def test_object_target(tool_app, tool_ws, engine) -> None:
    doc = open_doc(tool_ws)
    _export(tool_app, doc, [{"kind": "file", "format": "png", "object_id": "r1"}])
    assert engine[0][1] == "r1"


def test_profiles(tool_app, tool_ws, engine) -> None:
    doc = open_doc(tool_ws)
    icons = _export(tool_app, doc, [{"kind": "profile", "name": "icons", "sizes": [16, 32]}])
    assert [a["width_px"] for a in icons["artifacts"]] == [16, 32]
    assert icons["artifacts"][0]["kind"] == "profile:icons"
    web = _export(tool_app, doc, [{"kind": "profile", "name": "web", "widths": [320, 640]}])
    assert [a["format"] for a in web["artifacts"]] == ["png", "png", "svg"]


def test_save_as_writes_a_workspace_file(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    data = _export(tool_app, doc, [{"kind": "save_as", "dest_path": "out/copy.svg"}])
    assert data["saved"]["path"].endswith("copy.svg")
    assert (tool_ws / "out" / "copy.svg").is_file()


def test_save_as_overwrite_needs_approval(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    (tool_ws / "existing.svg").write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    target = [{"kind": "save_as", "dest_path": "existing.svg", "overwrite": True}]
    with pytest.raises(ToolError, match="approval_token"):
        _export(tool_app, doc, target)
    data = _export(tool_app, doc, target, approval_token="yes")
    assert data["saved"]["overwritten"] is True


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


def test_dry_run_plans_without_writing(tool_app, tool_ws, engine) -> None:
    doc = open_doc(tool_ws)
    data = _export(
        tool_app,
        doc,
        [{"kind": "file", "format": "png", "width_px": 128}, {"kind": "profile", "name": "web"}],
        dry_run=True,
    )
    assert data["dry_run"] is True and data["within_budget"] is True
    assert data["plan"][0]["format"] == "png" and data["projected_bytes"] > 0
    assert engine == []


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


def test_dry_run_totals_add_up_across_targets(tool_app, tool_ws, engine) -> None:
    doc = open_doc(tool_ws)
    data = _export(
        tool_app,
        doc,
        [{"kind": "file", "format": "png", "width_px": 128}, {"kind": "profile", "name": "print"}],
        dry_run=True,
    )
    assert data["projected_bytes"] == sum(p["projected_bytes"] for p in data["plan"])


def test_dry_run_rejects_what_the_real_run_would(tool_app, tool_ws, engine) -> None:
    doc = open_doc(tool_ws)
    with pytest.raises(ToolError, match="icon size"):
        _export(tool_app, doc, [{"kind": "profile", "name": "icons", "sizes": [0]}], dry_run=True)
    assert engine == []


def test_budget_refuses_an_oversized_batch(tool_app, tool_ws, engine) -> None:
    doc = open_doc(tool_ws)
    with pytest.raises(ToolError, match="budget"):
        _export(
            tool_app, doc, [{"kind": "file", "format": "png", "width_px": 4000}], byte_budget=10
        )
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


def test_failure_names_the_doc_and_target(tool_app, tool_ws, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RenderError("x")

    monkeypatch.setattr("inkscape_mcp.render.profiles.export_document", boom)
    doc = open_doc(tool_ws)
    with pytest.raises(ToolError, match=rf"{doc} targets\[0\] \(profile\): render/export failed"):
        _export(tool_app, doc, [{"kind": "profile", "name": "icons"}])


def test_out_dir_outside_the_workspace_is_rejected(tool_app, tool_ws, engine) -> None:
    doc = open_doc(tool_ws)
    with pytest.raises(ToolError, match="path rejected"):
        _export(tool_app, doc, [{"kind": "file", "format": "png"}], out_dir="../outside")
