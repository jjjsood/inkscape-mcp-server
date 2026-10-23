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

from inkscape_mcp.document import save as save_mod
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


@pytest.fixture(params=["dir_fd", "path_based"])
def fd_mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run a test on the fd-relative branch and on the path-based (Windows) one."""
    if request.param == "path_based":
        monkeypatch.setattr(save_mod, "HAS_DIR_FD_MKDIR", False)
    return str(request.param)


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
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(save_mod, "HAS_DIR_FD_MKDIR", False)  # import-time on real Windows
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


def _nested_doc(root: Path) -> str:
    (root / "run" / "WF7").mkdir(parents=True)
    return open_doc(root, "run/WF7/logo.svg", SVG)


def test_tool_foreign_dest_suggests_the_doc_folder(tool_app, tool_ws) -> None:
    doc_id = _nested_doc(tool_ws)
    with pytest.raises(ToolError, match=r"run/WF7/foreign\.svg"):
        _save_as(tool_app, doc_id, "WF7/foreign.svg")
    assert not (tool_ws / "WF7").exists()


def test_tool_existing_folder_and_absolute_dests_are_not_rejected(tool_app, tool_ws) -> None:
    doc_id = _nested_doc(tool_ws)
    (tool_ws / "out").mkdir()
    assert _save_as(tool_app, doc_id, "out/a.svg")["saved"]["path"] == "out/a.svg"
    data = _save_as(tool_app, doc_id, str(tool_ws / "new" / "b.svg"))
    assert data["saved"]["path"] == "new/b.svg"


def test_tool_bare_filename_at_root_gets_a_note(tool_app, tool_ws) -> None:
    doc_id = _nested_doc(tool_ws)
    saved = _save_as(tool_app, doc_id, "foreign.svg")["saved"]
    assert saved["path"] == "foreign.svg"
    assert "workspace root" in saved["note"] and "run/WF7/" in saved["note"]


# --- reversible overwrite: backup in the server store + history(action='restore_file') ----------

OLD = b"<svg>old</svg>"


def _restore_file(app, doc_id, operation_id, **kw):
    args = {"doc_id": doc_id, "action": "restore_file", "operation_id": operation_id, **kw}
    return call(app, "history", args).data


def test_overwrite_backs_up_old_bytes_outside_the_workspace_folder(doc) -> None:
    doc_id, root = doc
    dest = root / "existing.svg"
    dest.write_bytes(OLD)
    result = save_document_as(doc_id, str(dest), overwrite=True, approval_token="ok")
    assert result.undo == (
        f"history(doc_id='{doc_id}', action='restore_file', operation_id='{result.operation_id}')"
    )
    (rec,) = _save_records(root, doc_id)
    backup = sandbox.backups_dir(root, doc_id) / rec["backup_file"]
    assert backup.read_bytes() == OLD
    assert not any(p.name.endswith(".bak") for p in root.iterdir())  # nothing next to the file
    # a brand-new file has nothing to back up
    fresh = save_document_as(doc_id, str(root / "fresh.svg"))
    assert fresh.undo is None


def test_overwrite_undo_restores_old_bytes_and_is_itself_reversible(tool_app, doc, fd_mode) -> None:
    doc_id, root = doc
    dest = root / "taken.svg"
    dest.write_bytes(OLD)
    saved = _save_as(tool_app, doc_id, "taken.svg", overwrite=True, approval_token="yes")["saved"]
    assert dest.read_bytes() == SVG
    assert saved["undo"] == (
        f"history(doc_id='{doc_id}', action='restore_file', operation_id='{saved['operation_id']}')"
    )

    # refused without a token (and with a blank one); the file is untouched
    for kw in ({}, {"approval_token": " "}):
        with pytest.raises(ToolError, match="approval_token"):
            _restore_file(tool_app, doc_id, saved["operation_id"], **kw)
    assert dest.read_bytes() == SVG

    # dry_run: plan only, no token needed, nothing written
    plan = _restore_file(tool_app, doc_id, saved["operation_id"], dry_run=True)
    assert plan["dry_run"] is True and plan["risk"] == "high" and plan["needs_approval"] is True
    assert plan["plan"] == {
        "path": "taken.svg",
        "operation_id": saved["operation_id"],
        "bytes": len(OLD),
    }
    assert dest.read_bytes() == SVG

    undone = _restore_file(tool_app, doc_id, saved["operation_id"], approval_token="yes")
    assert dest.read_bytes() == OLD
    assert "working copy (doc_id) is unchanged" in undone["note"]
    assert undone["restored_path"] == "taken.svg" and undone["bytes"] == len(OLD)

    # the restore replaced the saved bytes, so it carries its own undo (redo of the save)
    again = _restore_file(tool_app, doc_id, undone["operation_id"], approval_token="yes")
    assert dest.read_bytes() == SVG
    assert again["undo"].endswith(f"operation_id='{again['operation_id']}')")


def test_restore_file_rejects_bad_operations_and_symlinked_destinations(tool_app, doc) -> None:
    doc_id, root = doc
    with pytest.raises(ToolError, match="operation not found"):
        _restore_file(tool_app, doc_id, "op_deadbeef", approval_token="yes")
    with pytest.raises(ToolError, match="operation not found"):
        _restore_file(tool_app, doc_id, "../../x", approval_token="yes")
    plain = save_document_as(doc_id, str(root / "plain.svg"))  # new file: no backup
    with pytest.raises(ToolError, match="no overwritten-file backup"):
        _restore_file(tool_app, doc_id, plain.operation_id, approval_token="yes")

    dest = root / "swap.svg"
    dest.write_bytes(OLD)
    saved = save_document_as(doc_id, str(dest), overwrite=True, approval_token="ok")
    target = root / "elsewhere.svg"
    target.write_bytes(b"<svg>keep</svg>")
    dest.unlink()
    dest.symlink_to(target)  # the destination became a link after the save
    with pytest.raises(ToolError, match="symbolic link"):
        _restore_file(tool_app, doc_id, saved.operation_id, approval_token="yes")
    assert target.read_bytes() == b"<svg>keep</svg>"


def test_prune_removes_old_backups_beyond_keep_n(doc) -> None:
    from datetime import UTC, datetime, timedelta

    from inkscape_mcp import retention
    from inkscape_mcp.config import Settings

    doc_id, root = doc
    dest = root / "r.svg"
    for _ in range(3):
        dest.write_bytes(OLD)
        save_document_as(doc_id, str(dest), overwrite=True, approval_token="ok")
    folder = sandbox.backups_dir(root, doc_id)
    assert len(list(folder.glob("*.bak"))) == 3
    later = datetime.now(UTC) + timedelta(days=90)
    cfg = Settings(snapshot_keep_n=1, snapshot_keep_days=30)
    retention.prune_snapshots_at(root, doc_id, cfg, now=later)
    assert len(list(folder.glob("*.bak"))) == 1


# --- hardening: failure rollback, exclusive create, special files, backups dir, retention -------


def _overwrite(doc_id, dest, **kw):
    return save_document_as(doc_id, str(dest), overwrite=True, approval_token="ok", **kw)


def test_failed_write_restores_old_bytes_and_discards_the_record(doc, monkeypatch) -> None:
    from inkscape_mcp.document import save as save_mod

    doc_id, root = doc
    dest = root / "existing.svg"
    dest.write_bytes(OLD)
    real = save_mod._write_all
    calls = []

    def flaky(handle, data):
        calls.append(1)
        if len(calls) == 1:  # the save itself: write half, then the disk fills up
            handle.write(data[:5])
            raise OSError(28, "no space")
        real(handle, data)  # the rollback

    monkeypatch.setattr(save_mod, "_write_all", flaky)
    with pytest.raises(ToolError, match="could not be written"):
        _overwrite(doc_id, dest)
    assert dest.read_bytes() == OLD
    (rec,) = _save_records(root, doc_id)
    assert rec["status"] == "discarded" and rec["backup_file"] is None
    assert not list(sandbox.backups_dir(root, doc_id).glob("*.bak"))


def test_failed_write_and_failed_rollback_keep_the_backup_reachable(doc, monkeypatch) -> None:
    from inkscape_mcp.document import save as save_mod

    doc_id, root = doc
    dest = root / "existing.svg"
    dest.write_bytes(OLD)

    def broken(handle, data):
        raise OSError(28, "no space")

    monkeypatch.setattr(save_mod, "_write_all", broken)
    with pytest.raises(ToolError, match="could not be written"):
        _overwrite(doc_id, dest)
    (rec,) = _save_records(root, doc_id)
    assert rec["status"] == "applied" and rec["backup_file"]
    assert (sandbox.backups_dir(root, doc_id) / rec["backup_file"]).read_bytes() == OLD


def test_file_appearing_after_the_risk_decision_is_not_truncated(doc, monkeypatch) -> None:
    from inkscape_mcp.document import save as save_mod

    doc_id, root = doc
    dest = root / "race.svg"
    real_new = save_mod.new_operation

    def racing(*a, **kw):
        dest.write_bytes(OLD)  # appears between the exists() check and the open
        return real_new(*a, **kw)

    monkeypatch.setattr(save_mod, "new_operation", racing)
    with pytest.raises(ToolError, match="destination already exists"):
        save_document_as(doc_id, str(dest))
    assert dest.read_bytes() == OLD
    (rec,) = _save_records(root, doc_id)
    assert rec["status"] == "discarded"


def test_hardlinked_destination_is_refused(doc) -> None:
    doc_id, root = doc
    link = root / "hl.svg"
    os.link(root / "logo.svg", link)  # shares an inode with the registered source
    with pytest.raises(ToolError, match="hard-linked"):
        _overwrite(doc_id, link)
    assert (root / "logo.svg").read_bytes() == SVG == link.read_bytes()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_fifo_destination_is_refused(doc) -> None:
    doc_id, root = doc
    fifo = root / "pipe.svg"
    os.mkfifo(fifo)
    with pytest.raises(ToolError, match="non-regular"):
        _overwrite(doc_id, fifo)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs O_NOFOLLOW")
def test_symlinked_backups_dir_is_refused_on_write_and_restore(tool_app, doc, fd_mode) -> None:
    doc_id, root = doc
    dest = root / "existing.svg"
    dest.write_bytes(OLD)
    saved = _overwrite(doc_id, dest)
    folder = sandbox.backups_dir(root, doc_id)
    outside = root / "outside"
    outside.mkdir()
    folder.rename(root / "moved-backups")
    folder.symlink_to(outside)
    with pytest.raises(ToolError, match="no longer available"):
        _restore_file(tool_app, doc_id, saved.operation_id, approval_token="yes")
    dest.write_bytes(OLD)
    with pytest.raises(ToolError, match="could not back up"):
        _overwrite(doc_id, dest)
    assert dest.read_bytes() == OLD and not list(outside.iterdir())


def test_junctioned_backups_dir_is_refused_without_dir_fd(doc, monkeypatch) -> None:
    """Windows: lstat reports a junction as a directory, so the path-based branch checks it."""
    doc_id, root = doc
    dest = root / "existing.svg"
    dest.write_bytes(OLD)
    monkeypatch.setattr(save_mod, "HAS_DIR_FD_MKDIR", False)
    monkeypatch.setattr(Path, "is_junction", lambda self: self.name == "backups")
    with pytest.raises(ToolError, match="could not back up"):
        _overwrite(doc_id, dest)
    assert dest.read_bytes() == OLD


def test_open_creates_the_backups_dir(doc) -> None:
    doc_id, root = doc
    assert sandbox.backups_dir(root, doc_id).is_dir()


def test_zero_byte_destination_still_gets_an_undo(doc) -> None:
    doc_id, root = doc
    dest = root / "empty.svg"
    dest.write_bytes(b"")
    result = _overwrite(doc_id, dest)
    assert result.undo is not None
    (rec,) = _save_records(root, doc_id)
    assert (sandbox.backups_dir(root, doc_id) / rec["backup_file"]).read_bytes() == b""


def test_restore_requires_a_real_applied_save_backup(tool_app, doc) -> None:
    doc_id, root = doc
    dest = root / "existing.svg"
    dest.write_bytes(OLD)
    saved = _overwrite(doc_id, dest)
    path = sandbox.operations_dir(root, doc_id) / f"{saved.operation_id}.json"
    rec = json.loads(path.read_text())
    for patch in ({"backup_file": "../x.bak"}, {"tool": "edit"}, {"status": "discarded"}):
        path.write_text(json.dumps({**rec, **patch}))
        with pytest.raises(ToolError, match="no overwritten-file backup"):
            _restore_file(tool_app, doc_id, saved.operation_id, approval_token="yes")
    with pytest.raises(ToolError, match="operation not found"):  # `$` used to accept a trailing \n
        _restore_file(tool_app, doc_id, saved.operation_id + "\n", approval_token="yes")


def _later():
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(hours=1)  # past the in-flight grace window


def _backups_with_records(doc, n, size=4):
    doc_id, root = doc
    dest = root / "r.svg"
    ops = []
    for _ in range(n):
        dest.write_bytes(b"x" * size)
        ops.append(_overwrite(doc_id, dest).operation_id)
    return doc_id, root, ops


def test_prune_hard_caps_apply_to_backups_and_clear_the_record_link(doc) -> None:
    from inkscape_mcp import retention
    from inkscape_mcp.config import Settings

    doc_id, root, ops = _backups_with_records(doc, 4)
    folder = sandbox.backups_dir(root, doc_id)
    for i, op in enumerate(ops):  # strictly increasing mtimes: ops[0] oldest
        os.utime(folder / f"{op}.bak", (1_000_000 + i, 1_000_000 + i))

    cfg = Settings(snapshot_keep_n=10, snapshot_keep_days=0, snapshot_hard_max_n=3)
    retention.prune_snapshots_at(root, doc_id, cfg)
    assert sorted(p.stem for p in folder.glob("*.bak")) == sorted(ops[1:])
    ops_dir = sandbox.operations_dir(root, doc_id)
    assert json.loads((ops_dir / f"{ops[0]}.json").read_text())["backup_file"] is None
    assert json.loads((ops_dir / f"{ops[1]}.json").read_text())["backup_file"] == f"{ops[1]}.bak"

    cfg = Settings(snapshot_keep_n=10, snapshot_keep_days=0, snapshot_hard_max_bytes=8)
    freed = retention.prune_snapshots_at(root, doc_id, cfg).freed_bytes
    assert freed == 4 and sorted(p.stem for p in folder.glob("*.bak")) == sorted(ops[2:])


def test_restore_of_a_pruned_backup_says_unavailable(tool_app, doc) -> None:
    from inkscape_mcp import retention
    from inkscape_mcp.config import Settings

    doc_id, root, ops = _backups_with_records(doc, 2)
    retention.prune_snapshots_at(
        root,
        doc_id,
        Settings(snapshot_keep_n=0, snapshot_keep_days=0, snapshot_hard_max_n=0),
        now=_later(),
    )
    with pytest.raises(ToolError, match="no overwritten-file backup"):
        _restore_file(tool_app, doc_id, ops[0], approval_token="yes")


def test_sweep_visits_docs_that_only_have_backups(doc) -> None:
    from inkscape_mcp import retention
    from inkscape_mcp.config import Settings

    doc_id, root, _ops = _backups_with_records(doc, 2)
    sandbox.snapshots_index(root, doc_id).unlink(missing_ok=True)
    cfg = Settings(
        workspace_roots=[root.resolve()], snapshot_keep_n=0, snapshot_keep_days=0,
        snapshot_hard_max_n=0,
    )  # fmt: skip
    result = retention.sweep_all_roots(cfg, now=_later())
    assert result.freed_bytes > 0
    assert not list(sandbox.backups_dir(root, doc_id).glob("*.bak"))


# --- second hardening pass ----------------------------------------------------------------------


def test_non_oserror_after_truncate_keeps_backup_and_names_the_undo(doc, monkeypatch) -> None:
    from inkscape_mcp.document import save as save_mod

    doc_id, root = doc
    dest = root / "existing.svg"
    dest.write_bytes(OLD)

    def boom(handle, data):
        raise ValueError("not an OSError")

    monkeypatch.setattr(save_mod, "_write_all", boom)  # the rollback write fails too
    with pytest.raises(ToolError, match="may be partial") as ei:
        _overwrite(doc_id, dest)
    (rec,) = _save_records(root, doc_id)
    assert f"operation_id='{rec['operation_id']}'" in str(ei.value)
    assert rec["status"] == "applied" and rec["backup_file"]
    assert (sandbox.backups_dir(root, doc_id) / rec["backup_file"]).read_bytes() == OLD


def test_restore_accepts_a_proposed_record_with_a_backup_and_binds_the_name(tool_app, doc) -> None:
    doc_id, root = doc
    dest = root / "existing.svg"
    dest.write_bytes(OLD)
    saved = _overwrite(doc_id, dest)
    path = sandbox.operations_dir(root, doc_id) / f"{saved.operation_id}.json"
    rec = json.loads(path.read_text())
    path.write_text(json.dumps({**rec, "status": "proposed"}))  # crash mid-write
    _restore_file(tool_app, doc_id, saved.operation_id, approval_token="yes")
    assert dest.read_bytes() == OLD
    # a backup name that is a valid shape but not this record's own is refused
    other = f"op_{'0' * 8}.bak"
    (sandbox.backups_dir(root, doc_id) / other).write_bytes(b"x")
    path.write_text(json.dumps({**rec, "backup_file": other}))
    with pytest.raises(ToolError, match="no overwritten-file backup"):
        _restore_file(tool_app, doc_id, saved.operation_id, approval_token="yes")


def test_prune_skips_young_and_non_applied_backups_and_keeps_link_on_write_failure(doc) -> None:
    from inkscape_mcp import retention
    from inkscape_mcp.config import Settings

    doc_id, root, ops = _backups_with_records(doc, 3)
    folder = sandbox.backups_dir(root, doc_id)
    ops_dir = sandbox.operations_dir(root, doc_id)
    cfg = Settings(snapshot_keep_n=0, snapshot_keep_days=0, snapshot_hard_max_n=0)
    retention.prune_snapshots_at(root, doc_id, cfg)  # all younger than the grace window
    assert len(list(folder.glob("*.bak"))) == 3

    rec_path = ops_dir / f"{ops[0]}.json"
    rec = json.loads(rec_path.read_text())
    rec_path.write_text(json.dumps({**rec, "status": "proposed"}))
    ro = ops_dir / f"{ops[1]}.json"
    ro.chmod(0o444)  # clearing the link will fail
    try:
        retention.prune_snapshots_at(root, doc_id, cfg, now=_later())
    finally:
        ro.chmod(0o644)
    assert sorted(p.stem for p in folder.glob("*.bak")) == sorted(ops[:2])  # proposed + unlinkable
    assert json.loads(ro.read_text())["backup_file"] == f"{ops[1]}.bak"


def test_sweep_skips_symlinked_and_oddly_named_doc_dirs(doc) -> None:
    from inkscape_mcp import retention
    from inkscape_mcp.config import Settings

    doc_id, root, _ops = _backups_with_records(doc, 1)
    docs = sandbox.documents_dir(root)
    real = sandbox.doc_dir(root, doc_id)
    outside = root / "outside"
    (outside / "backups").mkdir(parents=True)
    (outside / "backups" / "op_00000000.bak").write_bytes(b"x")
    (docs / "d_deadbeef").symlink_to(outside, target_is_directory=True)
    odd = docs / "not-a-doc"
    (odd / "backups").mkdir(parents=True)
    (odd / "backups" / "op_00000000.bak").write_bytes(b"x")
    cfg = Settings(
        workspace_roots=[root.resolve()], snapshot_keep_n=0, snapshot_keep_days=0,
        snapshot_hard_max_n=0,
    )  # fmt: skip
    result = retention.sweep_all_roots(cfg, now=_later())
    assert [d.doc_id for d in result.documents] == [doc_id]
    assert (outside / "backups" / "op_00000000.bak").exists()
    assert (odd / "backups" / "op_00000000.bak").exists()
    assert not list((real / "backups").glob("*.bak"))
