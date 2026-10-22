"""Save-as: `document.save.save_document_as` (engine) and the `export` `save_as` target.

Writes the working copy to a NEW workspace file. Overwriting needs `overwrite` plus an approval
token (HIGH risk); managed document files and symlinked destinations are never written; missing
parent folders are created only after a containment proof. No Inkscape involved.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.document.save import SaveResult, save_document_as
from inkscape_mcp.registry import get_registry
from inkscape_mcp.workspace import sandbox
from tests.conftest import run_ops
from tests.tool_support import call, open_doc

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" width="10" height="10"/>'
OUTSIDE = "path rejected: outside workspace"


@pytest.fixture
def doc(tool_ws: Path) -> tuple[str, Path]:
    """`(doc_id, workspace_root)` of an open `logo.svg`."""
    return open_doc(tool_ws, "logo.svg", SVG), tool_ws


def _save_records(root: Path, doc_id: str) -> list[dict]:
    files = sorted(sandbox.operations_dir(root, doc_id).glob("op_*.json"))
    records = [json.loads(p.read_text()) for p in files]
    return [r for r in records if r["tool"] == "save_document_as"]


# --- engine: new files ------------------------------------------------------------------------


def test_new_file_is_written_and_recorded(doc) -> None:
    doc_id, root = doc
    result = save_document_as(doc_id, str(root / "out.svg"))
    assert isinstance(result, SaveResult)
    assert (root / "out.svg").read_bytes() == SVG
    # a workspace-relative POSIX path, never absolute; both validation reports present
    assert result.saved_path == "out.svg" and result.overwritten is False
    assert result.pre_validation.ok is True and result.post_validation.ok is True
    assert result.pre_validation.doc_id == result.post_validation.doc_id == doc_id
    # source and working copy are untouched
    assert (root / "logo.svg").read_bytes() == SVG
    assert sandbox.working_copy(root, doc_id).read_bytes() == SVG
    (rec,) = _save_records(root, doc_id)
    assert (rec["status"], rec["risk_class"]) == ("applied", "medium")
    assert rec["artifacts"] == ["out.svg"] and rec["operation_id"] == result.operation_id


def test_save_persists_the_edited_working_copy_not_the_source(tool_ws) -> None:
    doc_id = open_doc(tool_ws, "edit.svg")  # default fixture SVG has rect r1
    run_ops(doc_id, {"op": "fill", "ids": ["r1"], "color": "#0000ff"})
    save_document_as(doc_id, "edited.svg")
    assert b"#0000ff" in (tool_ws / "edited.svg").read_bytes()
    assert b"#0000ff" not in (tool_ws / "edit.svg").read_bytes()


# --- engine: overwrite gating -----------------------------------------------------------------


def test_overwrite_without_approval_is_refused_and_leaves_the_file(doc) -> None:
    doc_id, root = doc
    dest = root / "existing.svg"
    dest.write_bytes(b"<svg/>")
    with pytest.raises(ToolError, match="approval_token"):  # overwrite defaults to False
        save_document_as(doc_id, str(dest))
    with pytest.raises(ToolError, match="approval_token"):
        save_document_as(doc_id, str(dest), overwrite=True)
    with pytest.raises(ToolError, match="approval_token"):
        save_document_as(doc_id, str(dest), overwrite=True, approval_token="")
    with pytest.raises(ToolError, match="approval_token"):  # a token alone is not consent
        save_document_as(doc_id, str(dest), approval_token="ok")
    assert dest.read_bytes() == b"<svg/>" and _save_records(root, doc_id) == []


def test_approved_overwrite_is_high_risk(doc) -> None:
    doc_id, root = doc
    dest = root / "existing.svg"
    dest.write_bytes(b"<svg/>")
    result = save_document_as(doc_id, str(dest), overwrite=True, approval_token="ok")
    assert result.overwritten is True and dest.read_bytes() == SVG
    (rec,) = _save_records(root, doc_id)
    assert (rec["status"], rec["risk_class"]) == ("applied", "high")
    assert rec["policy_decision"]["approved"] is True
    assert rec["policy_decision"]["approval_required"] is True


def test_managed_document_files_are_never_overwritten(doc) -> None:
    doc_id, root = doc
    entry = get_registry().get(doc_id)
    managed = [
        root / "logo.svg",  # the registered source
        sandbox.working_copy(root, doc_id),
        Path(entry.original_path),
    ]
    for path in managed:
        before = path.read_bytes()
        with pytest.raises(ToolError, match="managed"):
            save_document_as(doc_id, str(path), overwrite=True, approval_token="ok")
        assert path.read_bytes() == before


def test_another_documents_managed_files_are_never_overwritten(doc) -> None:
    doc_id, root = doc
    other = open_doc(root, "other.svg", SVG)
    entry = get_registry().get(other)
    for path in (sandbox.working_copy(root, other), Path(entry.original_path), root / "other.svg"):
        with pytest.raises(ToolError, match="managed"):
            save_document_as(doc_id, str(path), overwrite=True, approval_token="ok")
    assert _save_records(root, doc_id) == []


@pytest.mark.parametrize(
    "rel",
    [
        "{other}/new.svg",
        "{other}/working/new.svg",
        "{other}/snapshots/new.svg",
        "{other}/fresh/sub/new.svg",
        "{own}/new.svg",
    ],
)
def test_destinations_inside_a_managed_document_dir_are_refused(doc, rel) -> None:
    doc_id, root = doc
    other = open_doc(root, "other.svg", SVG)
    docs = root / sandbox.STATE_DIR / "documents"
    dest = docs / rel.format(other=other, own=doc_id)
    before = sorted(docs.rglob("*"))
    with pytest.raises(ToolError, match="managed"):
        save_document_as(doc_id, str(dest.relative_to(root)))
    assert sorted(docs.rglob("*")) == before  # no file and no parent folder was created
    assert _save_records(root, doc_id) == []


@pytest.mark.xfail(
    strict=True,
    reason="design question: save_as writes SVG bytes to any filename; no .svg/.svgz suffix "
    "rule exists in code or docs",
)
@pytest.mark.parametrize("dest", ["x.png", ".bashrc", "notes.txt"])
def test_destination_must_have_an_svg_suffix(doc, dest) -> None:
    doc_id, root = doc
    with pytest.raises(ToolError, match="svg"):
        save_document_as(doc_id, dest)
    assert not (root / dest).exists()


# --- engine: sandbox ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dest", ["../evil.svg", "../escape/x.svg", "/etc/inkscape_mcp_escape/x.svg", "a/../../b.svg"]
)
def test_escaping_destinations_are_rejected_and_create_nothing(doc, tool_ws, dest) -> None:
    doc_id, root = doc
    before = set(root.parent.iterdir())
    with pytest.raises(ToolError) as exc:
        save_document_as(doc_id, dest)
    assert str(exc.value) == OUTSIDE and str(root) not in str(exc.value)
    assert set(root.parent.iterdir()) == before
    assert not Path("/etc/inkscape_mcp_escape").exists() and not (root / "a").exists()


@pytest.mark.parametrize("form", ["absolute", "relative"])
def test_symlink_leading_out_of_the_workspace_is_rejected(doc, tmp_path, form) -> None:
    doc_id, root = doc
    target = tmp_path / "escape_target.svg"
    (root / "evil_link.svg").symlink_to(target)
    dest = str(root / "evil_link.svg") if form == "absolute" else "evil_link.svg"
    with pytest.raises(ToolError) as exc:  # even with overwrite + approval
        save_document_as(doc_id, dest, overwrite=True, approval_token="ok")
    assert str(exc.value) == OUTSIDE
    assert not target.exists()


def test_symlink_inside_the_workspace_is_refused_not_followed(doc) -> None:
    doc_id, root = doc
    (root / "real.svg").write_bytes(b"<svg/>")
    (root / "link.svg").symlink_to(root / "real.svg")
    with pytest.raises(ToolError, match="symbolic link"):
        save_document_as(doc_id, str(root / "link.svg"), overwrite=True, approval_token="ok")
    assert (root / "real.svg").read_bytes() == b"<svg/>"


def test_nested_destination_through_a_symlinked_folder_is_rejected(doc, tmp_path) -> None:
    doc_id, root = doc
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (root / "linked").symlink_to(outside)
    with pytest.raises(ToolError) as exc:
        save_document_as(doc_id, "linked/sub/out.svg")
    assert str(exc.value) == OUTSIDE
    assert not (outside / "sub").exists()


def test_symlink_into_the_state_dir_creates_nothing_there(doc) -> None:
    doc_id, root = doc
    (root / "link").symlink_to(root / sandbox.STATE_DIR)
    with pytest.raises(ToolError, match="managed"):
        save_document_as(doc_id, "link/new/f.svg")
    assert not (root / sandbox.STATE_DIR / "new").exists()


def test_save_works_without_o_nofollow(doc, monkeypatch) -> None:
    """Windows has no os.O_NOFOLLOW: no AttributeError, symlink still refused explicitly."""
    monkeypatch.delattr(os, "O_NOFOLLOW")
    doc_id, root = doc
    save_document_as(doc_id, "plain.svg")
    assert (root / "plain.svg").read_bytes() == SVG


# --- engine: relative destinations -----------------------------------------------------------


@pytest.mark.parametrize(
    "dest", ["flat_out.svg", "sub/out.svg", "output/final.svg", "a/b/c/final.svg"]
)
def test_relative_destinations_anchor_to_the_workspace_and_create_folders(
    doc, tmp_path, monkeypatch, dest
) -> None:
    doc_id, root = doc
    (root / "sub").mkdir()  # an existing parent works too
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = save_document_as(doc_id, dest)
    assert (root / dest).read_bytes() == SVG
    assert result.saved_path == dest and result.overwritten is False
    assert list(elsewhere.iterdir()) == []  # nothing relative to the process CWD
    (rec,) = _save_records(root, doc_id)
    assert (rec["status"], rec["risk_class"], rec["artifacts"]) == ("applied", "medium", [dest])


def test_unknown_document(tool_ws) -> None:
    with pytest.raises(ToolError, match="document id not found"):
        save_document_as("d_nope", "out.svg")


# --- through the `export` tool ----------------------------------------------------------------


def _save_as(app, doc_id, dest, **kw):
    overwrite = kw.pop("overwrite", False)
    target = {"kind": "save_as", "dest_path": dest, "overwrite": overwrite}
    return call(app, "export", {"doc_id": doc_id, "targets": [target], **kw}).data


def test_tool_save_as_reports_path_and_operation(tool_app, doc) -> None:
    doc_id, root = doc
    data = _save_as(tool_app, doc_id, "output/final.svg")
    assert "artifacts" not in data and data["count"] == 1 and data["total_bytes"] == len(SVG)
    assert data["saved"]["path"] == "output/final.svg" and "overwritten" not in data["saved"]
    (rec,) = _save_records(root, doc_id)
    assert data["saved"]["operation_id"] == rec["operation_id"]
    assert (root / "output" / "final.svg").read_bytes() == SVG


def test_tool_save_as_dry_run_plans_without_writing(tool_app, doc) -> None:
    doc_id, root = doc
    data = call(
        tool_app,
        "export",
        {
            "doc_id": doc_id,
            "targets": [{"kind": "save_as", "dest_path": "later.svg", "overwrite": True}],
            "dry_run": True,
        },
    ).data
    assert data["plan"] == [{"save_as": "later.svg", "overwrite": True}]
    assert not (root / "later.svg").exists() and _save_records(root, doc_id) == []


def test_tool_save_as_errors_name_doc_and_target(tool_app, doc) -> None:
    doc_id, root = doc
    (root / "taken.svg").write_bytes(b"<svg/>")
    prefix = rf"^{doc_id} targets\[0\] \(save_as\): "
    with pytest.raises(ToolError, match=prefix + "destination already exists"):
        _save_as(tool_app, doc_id, "taken.svg")
    with pytest.raises(ToolError, match=prefix + OUTSIDE):
        _save_as(tool_app, doc_id, "../evil.svg", approval_token="yes")
    with pytest.raises(ToolError, match=prefix + "cannot overwrite a managed document file"):
        _save_as(tool_app, doc_id, "logo.svg", overwrite=True, approval_token="yes")
    assert (root / "taken.svg").read_bytes() == b"<svg/>" and not (
        root.parent / "evil.svg"
    ).exists()


def test_tool_overwrite_needs_both_flag_and_token(tool_app, doc) -> None:
    doc_id, root = doc
    (root / "taken.svg").write_bytes(b"<svg/>")
    with pytest.raises(ToolError, match="approval_token"):
        _save_as(tool_app, doc_id, "taken.svg", overwrite=True)
    with pytest.raises(ToolError, match="approval_token"):
        _save_as(tool_app, doc_id, "taken.svg", approval_token="yes")
    assert (root / "taken.svg").read_bytes() == b"<svg/>"
    data = _save_as(tool_app, doc_id, "taken.svg", overwrite=True, approval_token="yes")
    assert data["saved"]["overwritten"] is True
    assert (root / "taken.svg").read_bytes() == SVG
