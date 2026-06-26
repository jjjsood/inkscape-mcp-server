"""save_document_as tool tests (E2-05): new-file save, overwrite gating, sandbox + validation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.server import mcp
from inkscape_mcp.tools.save import SaveResult, save_document_as
from inkscape_mcp.workspace import sandbox

# A minimal valid SVG with a viewBox so validation has no errors (ok == True).
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" width="10" height="10"/>'


@pytest.fixture
def doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path, Path]:
    """Open a fixture SVG; return (doc_id, owning_root, original_source_path)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    src = ws / "logo.svg"
    src.write_bytes(SVG)
    entry = get_registry().open_document(str(src))
    return entry.doc_id, ws, src


def _operation_records(root: Path, doc_id: str) -> list[dict]:
    """Load all Operation Record JSON files for a document."""
    op_dir = sandbox.operations_dir(root, doc_id)
    return [json.loads(p.read_text()) for p in sorted(op_dir.glob("op_*.json"))]


def test_new_file_save_writes_relative_path_original_unchanged(
    doc: tuple[str, Path, Path],
) -> None:
    doc_id, root, src = doc
    dest = root / "out.svg"

    result = save_document_as(doc_id, str(dest))

    # The new file landed inside the workspace with the working-copy bytes.
    assert dest.is_file()
    assert dest.read_bytes() == SVG

    # Result shape: relative POSIX path (not absolute), not overwritten, both reports present.
    assert isinstance(result, SaveResult)
    assert result.saved_path == "out.svg"
    assert not Path(result.saved_path).is_absolute()
    assert result.overwritten is False
    assert result.pre_validation.doc_id == doc_id
    assert result.post_validation.doc_id == doc_id
    assert result.pre_validation.ok is True
    assert result.post_validation.ok is True

    # Original source byte-unchanged; working copy byte-unchanged.
    assert src.read_bytes() == SVG
    assert sandbox.working_copy(root, doc_id).read_bytes() == SVG

    # An applied, medium-risk Operation Record exists referencing the saved artifact.
    records = _operation_records(root, doc_id)
    save_recs = [r for r in records if r["tool"] == "save_document_as"]
    assert len(save_recs) == 1
    rec = save_recs[0]
    assert rec["status"] == "applied"
    assert rec["risk_class"] == "medium"
    assert rec["artifacts"] == ["out.svg"]
    assert rec["operation_id"] == result.operation_id


def test_overwrite_without_approval_rejected_dest_unchanged(
    doc: tuple[str, Path, Path],
) -> None:
    doc_id, root, _ = doc
    dest = root / "existing.svg"
    sentinel = b"<svg/>"
    dest.write_bytes(sentinel)

    # overwrite defaults False → rejected.
    with pytest.raises(ToolError) as exc1:
        save_document_as(doc_id, str(dest))
    assert "approval" in str(exc1.value).lower() or "overwrit" in str(exc1.value).lower()

    # overwrite=True but no approval_token → still rejected.
    with pytest.raises(ToolError):
        save_document_as(doc_id, str(dest), overwrite=True)

    # overwrite=True with an empty-string token → still rejected.
    with pytest.raises(ToolError):
        save_document_as(doc_id, str(dest), overwrite=True, approval_token="")

    # Destination bytes were never touched.
    assert dest.read_bytes() == sentinel


def test_overwrite_with_approval_succeeds_high_risk_approved(
    doc: tuple[str, Path, Path],
) -> None:
    doc_id, root, _ = doc
    dest = root / "existing.svg"
    dest.write_bytes(b"<svg/>")

    result = save_document_as(doc_id, str(dest), overwrite=True, approval_token="ok")

    assert result.overwritten is True
    assert dest.read_bytes() == SVG
    assert result.saved_path == "existing.svg"

    records = _operation_records(root, doc_id)
    save_recs = [r for r in records if r["tool"] == "save_document_as"]
    assert len(save_recs) == 1
    rec = save_recs[0]
    assert rec["status"] == "applied"
    assert rec["risk_class"] == "high"
    assert rec["policy_decision"]["approved"] is True
    assert rec["policy_decision"]["approval_required"] is True


def test_refuse_to_overwrite_managed_files(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, src = doc
    entry = get_registry().get(doc_id)

    # The registered source file is managed → refused (even though it exists outside the
    # per-document state dir, it is a tracked document file).
    src_before = src.read_bytes()
    with pytest.raises(ToolError) as exc:
        save_document_as(doc_id, str(src), overwrite=True, approval_token="ok")
    assert "managed" in str(exc.value).lower()
    assert src.read_bytes() == src_before

    # The internal working copy is managed → refused.
    working = sandbox.working_copy(root, doc_id)
    working_before = working.read_bytes()
    with pytest.raises(ToolError):
        save_document_as(doc_id, str(working), overwrite=True, approval_token="ok")
    assert working.read_bytes() == working_before

    # The original.svg copy is managed → refused.
    original = Path(entry.original_path)
    original_before = original.read_bytes()
    with pytest.raises(ToolError):
        save_document_as(doc_id, str(original), overwrite=True, approval_token="ok")
    assert original.read_bytes() == original_before


def test_path_escape_rejected_nothing_written_outside(
    doc: tuple[str, Path, Path], tmp_path: Path
) -> None:
    doc_id, root, _ = doc

    # An absolute path outside the workspace root.
    outside = tmp_path / "evil.svg"
    with pytest.raises(ToolError):
        save_document_as(doc_id, str(outside))
    assert not outside.exists()

    # A relative-traversal path that resolves outside the sandbox.
    with pytest.raises(ToolError):
        save_document_as(doc_id, str(root / ".." / "evil2.svg"))
    assert not (root.parent / "evil2.svg").exists()


def test_symlinked_dest_escape_rejected_no_write_to_target(
    doc: tuple[str, Path, Path], tmp_path: Path
) -> None:
    """E10-01 / SV5: a pre-existing symlink AT the dest name pointing OUTSIDE the workspace is
    rejected even with overwrite=True + an approval token, and nothing is written to the link
    target."""
    doc_id, root, _ = doc

    # The link target lives outside the workspace root and does not yet exist.
    escape_target = tmp_path / "escape_target.svg"
    assert not escape_target.exists()

    # A symlink INSIDE the workspace whose final name points at the out-of-sandbox target.
    evil_link = root / "evil_link.svg"
    evil_link.symlink_to(escape_target)

    with pytest.raises(ToolError) as exc:
        save_document_as(doc_id, str(evil_link), overwrite=True, approval_token="ok")
    assert str(exc.value) == "path rejected: outside workspace"

    # The escape did not happen: no file appeared at the link target.
    assert not escape_target.exists()


def test_symlinked_dest_escape_rejected_relative_form(
    doc: tuple[str, Path, Path], tmp_path: Path
) -> None:
    """Same escape but addressed via a RELATIVE dest name (anchored to the workspace root)."""
    doc_id, root, _ = doc

    escape_target = tmp_path / "escape_target2.svg"
    (root / "evil_rel.svg").symlink_to(escape_target)

    with pytest.raises(ToolError) as exc:
        save_document_as(doc_id, "evil_rel.svg", overwrite=True, approval_token="ok")
    assert str(exc.value) == "path rejected: outside workspace"
    assert not escape_target.exists()


def test_in_sandbox_symlinked_dest_refused_not_followed(
    doc: tuple[str, Path, Path],
) -> None:
    """An IN-sandbox symlink at the dest is refused with a clear message, never written through."""
    doc_id, root, _ = doc
    real = root / "real.svg"
    real.write_bytes(b"<svg/>")
    link = root / "link.svg"
    link.symlink_to(real)

    with pytest.raises(ToolError) as exc:
        save_document_as(doc_id, str(link), overwrite=True, approval_token="ok")
    assert "symbolic link" in str(exc.value).lower()
    # The link's real target was not overwritten with the working-copy bytes.
    assert real.read_bytes() == b"<svg/>"


def test_relative_dest_anchors_to_workspace_root_not_cwd(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """E10-08 SV1: a relative dest resolves against the workspace ROOT, not the process CWD."""
    doc_id, root, _ = doc

    # Put the process CWD somewhere OTHER than the workspace root so the two bases differ.
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    # A flat relative name lands directly under the workspace root, never under the CWD.
    result = save_document_as(doc_id, "flat_out.svg")
    assert (root / "flat_out.svg").is_file()
    assert (root / "flat_out.svg").read_bytes() == SVG
    assert result.saved_path == "flat_out.svg"
    assert not (other_cwd / "flat_out.svg").exists()


def test_relative_nested_dest_with_existing_parent_succeeds(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relative nested dest whose parent already exists anchors under the workspace root,
    not the process CWD."""
    doc_id, root, _ = doc
    other_cwd = tmp_path / "elsewhere2"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    (root / "sub").mkdir()
    result = save_document_as(doc_id, "sub/out.svg")
    assert (root / "sub" / "out.svg").read_bytes() == SVG
    assert result.saved_path == "sub/out.svg"
    assert not (other_cwd / "sub").exists()


def test_relative_nested_dest_creates_missing_subfolder(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-user delight (the reported failure): a relative dest into a subfolder that does not
    exist yet CREATES the subfolder under the workspace root and the saved file opens. The dir
    is created under the root, never against the process CWD."""
    doc_id, root, _ = doc
    other_cwd = tmp_path / "elsewhere3"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    assert not (root / "output").exists()
    result = save_document_as(doc_id, "output/final.svg")

    # The subfolder was created under the workspace root and the file is readable there.
    assert (root / "output").is_dir()
    saved = root / "output" / "final.svg"
    assert saved.is_file()
    assert saved.read_bytes() == SVG
    assert result.saved_path == "output/final.svg"
    assert result.overwritten is False
    # Nothing was created against the process CWD.
    assert not (other_cwd / "output").exists()

    # An applied, medium-risk Operation Record references the saved artifact at its relative path.
    records = _operation_records(root, doc_id)
    save_recs = [r for r in records if r["tool"] == "save_document_as"]
    assert len(save_recs) == 1
    assert save_recs[0]["status"] == "applied"
    assert save_recs[0]["risk_class"] == "medium"
    assert save_recs[0]["artifacts"] == ["output/final.svg"]


def test_relative_deep_nested_dest_creates_full_chain(
    doc: tuple[str, Path, Path],
) -> None:
    """A deeper dest `"a/b/c/final.svg"` creates the whole intermediate chain under the root."""
    doc_id, root, _ = doc

    assert not (root / "a").exists()
    result = save_document_as(doc_id, "a/b/c/final.svg")

    saved = root / "a" / "b" / "c" / "final.svg"
    assert saved.is_file()
    assert saved.read_bytes() == SVG
    assert result.saved_path == "a/b/c/final.svg"


def test_out_of_sandbox_nested_dest_rejected_creates_nothing(
    doc: tuple[str, Path, Path], tmp_path: Path
) -> None:
    """An out-of-sandbox nested dest is STILL rejected with `path rejected: outside workspace`
    and creates nothing outside the workspace — both a relative `..`-escape and an absolute path
    into a system directory."""
    doc_id, root, _ = doc

    # Relative escape via `..` into a subfolder above the workspace root.
    with pytest.raises(ToolError) as exc1:
        save_document_as(doc_id, "../escape/x.svg")
    assert str(exc1.value) == "path rejected: outside workspace"
    assert not (root.parent / "escape").exists()

    # Absolute path into a system directory (parent exists, but resolves outside every root).
    with pytest.raises(ToolError) as exc2:
        save_document_as(doc_id, "/etc/inkscape_mcp_escape/x.svg")
    assert str(exc2.value) == "path rejected: outside workspace"
    assert not Path("/etc/inkscape_mcp_escape").exists()


def test_nested_dest_through_symlinked_ancestor_rejected(
    doc: tuple[str, Path, Path], tmp_path: Path
) -> None:
    """A nested dest whose existing ancestor is a symlink pointing OUTSIDE the workspace is
    rejected and creates nothing through the link (the pre-create containment proof catches it)."""
    doc_id, root, _ = doc

    outside = tmp_path / "outside_dir"
    outside.mkdir()
    # An in-workspace name that is actually a symlink to an out-of-sandbox directory.
    (root / "linked").symlink_to(outside)

    with pytest.raises(ToolError) as exc:
        save_document_as(doc_id, "linked/sub/out.svg")
    assert str(exc.value) == "path rejected: outside workspace"
    # Nothing was created through the link target.
    assert not (outside / "sub").exists()


def test_unknown_doc_id_maps_to_toolerror(doc: tuple[str, Path, Path]) -> None:
    _, root, _ = doc
    with pytest.raises(ToolError) as exc:
        save_document_as("d_nope", str(root / "out.svg"))
    assert "not found" in str(exc.value)


def test_tool_registered_on_mcp(doc: tuple[str, Path, Path]) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "save_document_as" in names
