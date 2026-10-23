"""UX fixes: `export` output folder defaults and reporting."""

from __future__ import annotations

from pathlib import Path

import pytest

from inkscape_mcp.registry import get_registry
from tests.test_export_files import ART, FakeInkscape, install_fake
from tests.tool_support import call, open_doc

PNG = [{"kind": "file", "format": "png", "width_px": 40}]


@pytest.fixture
def fake_inkscape(monkeypatch: pytest.MonkeyPatch, tool_ws: Path) -> FakeInkscape:
    return install_fake(monkeypatch)


def _export(app, doc, targets=PNG, **kw):
    return call(app, "export", {"doc_id": doc, "targets": targets, **kw}).data


def test_export_dry_run_flags_an_overwriting_save_as(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws)
    (tool_ws / "taken.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    over = [{"kind": "save_as", "dest_path": "taken.svg", "overwrite": True}]
    plan = _export(tool_app, doc, over, dry_run=True)
    assert plan["risk"] == "high" and plan["needs_approval"] is True
    fresh = _export(tool_app, doc, [{"kind": "save_as", "dest_path": "new.svg"}], dry_run=True)
    assert fresh["risk"] == "medium" and "needs_approval" not in fresh


def test_export_defaults_next_to_the_source_file(tool_app, tool_ws, fake_inkscape) -> None:
    (tool_ws / "sub").mkdir()
    a = open_doc(tool_ws, "sub/a.svg", ART)
    b = open_doc(tool_ws, "b.svg", ART)
    res = _export(tool_app, [a, b])
    paths = {x["doc_id"]: x["path"] for x in res["artifacts"]}
    assert paths[a].startswith("sub/") and "/" not in paths[b]
    assert (tool_ws / paths[a]).is_file() and (tool_ws / paths[b]).is_file()
    assert "sub" in res["out_dir"] and "out_dir_note" not in res
    one = _export(tool_app, a)
    assert one["out_dir"] == "sub (defaulted to the source file's folder)"


def test_explicit_out_dir_and_save_as_still_win(tool_app, tool_ws, fake_inkscape) -> None:
    doc = open_doc(tool_ws, "a.svg", ART)
    res = _export(tool_app, doc, out_dir="other")
    assert res["artifacts"][0]["path"].startswith("other/") and res["out_dir"] == "other"
    res = _export(tool_app, doc, [*PNG, {"kind": "save_as", "dest_path": "dist/a2.svg"}])
    assert res["artifacts"][0]["path"].startswith("dist/")
    assert "save_as folder" in res["out_dir"]


def test_canvas_doc_uses_managed_store_and_says_so(tool_app, tool_ws, fake_inkscape) -> None:
    doc = call(tool_app, "open", {"canvas": {"width": 50, "height": 50}}).data["doc_id"]
    assert get_registry().get(doc).source_path == "document.svg"
    res = _export(tool_app, doc)
    assert res["artifacts"][0]["path"].startswith(".inkscape-mcp/")
    assert res["out_dir"].startswith(".inkscape-mcp/")
    assert "pass out_dir=" in res["out_dir_note"]


def test_created_doc_files_are_named_by_doc_id_and_save_as(
    tool_app, tool_ws, fake_inkscape
) -> None:
    doc = call(tool_app, "open", {"canvas": {"width": 50, "height": 50}}).data["doc_id"]
    name = Path(_export(tool_app, doc)["artifacts"][0]["path"]).name
    assert f"-{doc}-40px.png" in name and "-document-" not in name
    targets = [
        {"kind": "file", "format": "pdf"},
        {"kind": "save_as", "dest_path": "sheets/contact.svg"},
    ]
    path = _export(tool_app, doc, targets)["artifacts"][0]["path"]
    assert path.startswith("sheets/") and path.endswith(f"-contact-{doc}-auto.pdf")


def test_out_dir_escape_is_still_refused(tool_app, tool_ws, fake_inkscape) -> None:
    from fastmcp.exceptions import ToolError

    doc = open_doc(tool_ws, "a.svg", ART)
    with pytest.raises(ToolError):
        _export(tool_app, doc, out_dir="../escape")
