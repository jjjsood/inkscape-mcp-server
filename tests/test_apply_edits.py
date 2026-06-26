"""Typed DOM-edit batch tool tests: ``apply_edits``.

Proves the four load-bearing guarantees the backlog requires of the batch tool:

1. validate-all-first — one invalid member leaves the document BYTE-IDENTICAL (no write);
2. atomic rollback — a member that fails mid-batch persists NO partial mutation;
3. exactly ONE snapshot + ONE Operation Record per batch, and `restore_snapshot` reverts it all;
4. effective risk = MAX over members — a `delete_object` member forces the `approval_token` gate.

Hermetic: the pipeline's preview rendering is monkeypatched so no Inkscape is invoked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from lxml import etree

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render.cli import RenderResult
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.tools.batch import apply_edits
from inkscape_mcp.workspace import sandbox

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
    b'<rect id="r1" x="0" y="0" width="10" height="10" fill="#ff0000"/>'
    b'<text id="t1" x="0" y="20">hello</text>'
    b"</svg>"
)

SVG_NS = "http://www.w3.org/2000/svg"


def _fake_render_preview(doc_id: str, width_px: int | None = None) -> RenderResult:
    reg = get_registry()
    entry = reg.get(doc_id)
    root = Path(entry.root)
    preview_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    descriptor = "auto" if width_px is None else f"{width_px}px"
    produced = preview_dir / f"preview-{descriptor}.png"
    produced.write_bytes(b"\x89PNG\r\n\x1a\n")
    rel = produced.relative_to(root).as_posix()
    return RenderResult(
        doc_id=doc_id,
        artifact_path=rel,
        workspace_relative_path=rel,
        format="png",
        width_px=10,
        height_px=10,
        duration_s=0.0,
    )


@pytest.fixture
def doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    monkeypatch.setattr("inkscape_mcp.edit.pipeline.render_preview", _fake_render_preview)
    src = ws / "logo.svg"
    src.write_bytes(SVG)
    entry = get_registry().open_document(str(src))
    return entry.doc_id, ws, src


def _working_root(doc_id: str, root: Path) -> etree._Element:
    return etree.fromstring(sandbox.working_copy(root, doc_id).read_bytes())


def _find(root: etree._Element, object_id: str) -> etree._Element | None:
    for elem in root.iter():
        if isinstance(elem.tag, str) and elem.get("id") == object_id:
            return elem
    return None


def _snapshot_count(root: Path, doc_id: str) -> int:
    """Number of minted snapshots from the authoritative index (0 if none yet)."""
    index = sandbox.snapshots_index(root, doc_id)
    if not index.is_file():
        return 0
    return len(json.loads(index.read_text()))


def _style(elem: etree._Element) -> dict[str, str]:
    decls: dict[str, str] = {}
    for part in (elem.get("style") or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            decls[k.strip()] = v.strip()
    return decls


# --- 1. happy path: several edits, one snapshot + one record ------------------


def test_batch_applies_all_edits_under_one_operation(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc

    result = apply_edits(
        doc_id,
        [
            {"op": "set_fill", "object_ids": ["r1"], "color": "#0000ff"},
            {"op": "move_object", "object_id": "r1", "dx": 5, "dy": 0},
            {"op": "create_rect", "x": 50, "y": 50, "width": 20, "height": 20, "object_id": "bg"},
        ],
    )

    assert result.changed is True
    assert result.edit_count == 3
    assert result.risk_class == "medium"

    working = _working_root(doc_id, root)
    assert _style(_find(working, "r1"))["fill"] == "#0000ff"
    assert _find(working, "r1").get("transform") == "translate(5,0)"
    assert _find(working, "bg") is not None

    # Exactly one Operation Record for the whole batch.
    op_file = sandbox.operations_dir(root, doc_id) / f"{result.operation_id}.json"
    record = json.loads(op_file.read_text())
    assert record["tool"] == "apply_edits"
    assert record["status"] == "applied"
    assert record["risk_class"] == "medium"
    assert record["snapshot_id"] == result.snapshot_id

    # Exactly one snapshot directory for this doc.
    assert _snapshot_count(root, doc_id) == 1


# --- 2. validate-all-first: a bad member leaves the doc byte-identical --------


def test_invalid_member_leaves_document_byte_identical(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()

    with pytest.raises(ToolError):
        apply_edits(
            doc_id,
            [
                {"op": "set_fill", "object_ids": ["r1"], "color": "#00ff00"},
                {"op": "set_fill", "object_ids": ["r1"], "color": "notacolor;}"},
            ],
        )

    # No write, no snapshot, no Operation Record.
    assert sandbox.working_copy(root, doc_id).read_bytes() == before
    assert _snapshot_count(root, doc_id) == 0
    assert not list((sandbox.operations_dir(root, doc_id)).glob("*.json"))


# --- 3. atomic rollback: a member failing mid-batch persists nothing ----------


def test_failed_member_rolls_back_atomically(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()

    # First member is valid and would mutate; the second targets a missing id and fails at apply.
    with pytest.raises(ToolError):
        apply_edits(
            doc_id,
            [
                {"op": "set_fill", "object_ids": ["r1"], "color": "#123456"},
                {"op": "move_object", "object_id": "does-not-exist", "dx": 1, "dy": 1},
            ],
        )

    assert sandbox.working_copy(root, doc_id).read_bytes() == before
    assert _snapshot_count(root, doc_id) == 0


# --- 4. reversibility: one restore reverts the whole batch --------------------


def test_restore_snapshot_reverts_whole_batch(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()

    result = apply_edits(
        doc_id,
        [
            {"op": "set_fill", "object_ids": ["r1"], "color": "#0000ff"},
            {"op": "create_circle", "cx": 5, "cy": 5, "r": 3, "object_id": "c1"},
        ],
    )
    assert result.changed is True
    assert sandbox.working_copy(root, doc_id).read_bytes() != before

    restore_snapshot(doc_id, result.snapshot_id)
    assert sandbox.working_copy(root, doc_id).read_bytes() == before


# --- 5. risk = max over members: delete forces the approval gate --------------


def test_delete_member_requires_approval_token(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()

    # A batch containing a delete is HIGH; without a token the policy gate refuses it.
    with pytest.raises(ToolError, match="approval"):
        apply_edits(
            doc_id,
            [
                {"op": "set_fill", "object_ids": ["r1"], "color": "#0000ff"},
                {"op": "delete_object", "object_ids": ["t1"]},
            ],
        )
    # Refused before any write.
    assert sandbox.working_copy(root, doc_id).read_bytes() == before
    assert not list((sandbox.operations_dir(root, doc_id)).glob("*.json"))


def test_delete_member_applies_with_approval_token(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc

    result = apply_edits(
        doc_id,
        [
            {"op": "set_fill", "object_ids": ["r1"], "color": "#0000ff"},
            {"op": "delete_object", "object_ids": ["t1"]},
        ],
        approval_token="ok-123",
    )

    assert result.changed is True
    assert result.risk_class == "high"
    working = _working_root(doc_id, root)
    assert _find(working, "t1") is None
    assert _style(_find(working, "r1"))["fill"] == "#0000ff"

    record = json.loads(
        (sandbox.operations_dir(root, doc_id) / f"{result.operation_id}.json").read_text()
    )
    assert record["risk_class"] == "high"


# --- 6. empty / no-op batches -------------------------------------------------


def test_empty_batch_is_rejected(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, _ = doc
    with pytest.raises(ToolError, match="at least one"):
        apply_edits(doc_id, [])


def test_all_noop_batch_writes_nothing(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    # r1 is already #ff0000; setting it to the same colour is a no-op for every member.
    result = apply_edits(
        doc_id,
        [{"op": "set_fill", "object_ids": ["r1"], "color": "#ff0000"}],
    )
    assert result.changed is False
    assert result.operation_id == ""
    assert result.snapshot_id == ""
    assert _snapshot_count(root, doc_id) == 0


def test_unknown_document_maps_to_tool_error(doc: tuple[str, Path, Path]) -> None:
    with pytest.raises(ToolError, match="document id not found"):
        apply_edits("nope", [{"op": "normalize_viewbox"}])
