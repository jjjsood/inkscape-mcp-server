"""Object ops of `edit`: duplicate, rename, delete (HIGH risk) and their approval gate.

Hermetic. A rejected call leaves the working copy byte-identical and writes no snapshot; a
deleted object is recovered byte-for-byte by restoring the call's snapshot.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from lxml import etree

from inkscape_mcp.document.inspect import INKSCAPE_NS, SVG_NS, XLINK_NS
from inkscape_mcp.edit.ops import OpError
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.workspace import sandbox
from tests.conftest import run_ops
from tests.tool_support import open_doc

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" '
    b'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
    b'xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="100">'
    b'<text id="t1">old</text>'
    b'<g id="g1"><rect id="r1"/></g>'
    b'<use id="u1" href="#r1"/>'
    b'<rect id="seed1" x="20" width="10" height="10"/>'
    b'<rect id="seed2" x="40" width="10" height="10"/>'
    b"</svg>"
)

TOKEN = "approve-delete-1"


@pytest.fixture
def doc(tool_ws: Path) -> str:
    return open_doc(tool_ws, "logo.svg", SVG)


def _working(root: Path, doc_id: str) -> bytes:
    return sandbox.working_copy(root, doc_id).read_bytes()


def _tree(root: Path, doc_id: str) -> etree._Element:
    return etree.fromstring(_working(root, doc_id))


def _find(tree: etree._Element, oid: str) -> etree._Element | None:
    return next((e for e in tree.iter() if isinstance(e.tag, str) and e.get("id") == oid), None)


def _ids(root: Path, doc_id: str) -> set[str]:
    tree = _tree(root, doc_id)
    return {e.get("id") for e in tree.iter() if isinstance(e.tag, str) and e.get("id")}


def _counts(root: Path, doc_id: str) -> tuple[int, int]:
    """(snapshots, non-discarded operation records) written so far."""
    snaps = sandbox.snapshots_dir(root, doc_id)
    ops = sandbox.operations_dir(root, doc_id)
    records = [json.loads(p.read_text()) for p in ops.glob("op_*.json")] if ops.exists() else []
    return (
        len(list(snaps.glob("*.svg"))) if snaps.exists() else 0,
        sum(r["status"] != "discarded" for r in records),
    )


def _rejected(root: Path, doc_id: str, *ops: dict[str, Any], match: str | None = None, **kw: Any):
    before, counts = _working(root, doc_id), _counts(root, doc_id)
    with pytest.raises(OpError, match=match):
        run_ops(doc_id, *ops, **kw)
    assert _working(root, doc_id) == before
    assert _counts(root, doc_id) == counts


def _delete(ids: list[str]) -> dict[str, Any]:
    return {"op": "delete", "ids": ids}


# --- duplicate -----------------------------------------------------------------------


def test_duplicate_inserts_a_unique_sibling_right_after_the_original(tool_ws, doc) -> None:
    result = run_ops(doc, {"op": "duplicate", "ids": ["g1"]})
    tree = _tree(tool_ws, doc)
    g1 = _find(tree, "g1")
    parent = g1.getparent()
    clone = list(parent)[list(parent).index(g1) + 1]
    clone_id = clone.get("id")
    assert clone_id not in (None, "g1")
    assert result.created[0].id == clone_id and clone_id in result.summary
    # descendants are re-id'd too, so no id occurs twice
    assert clone.find(f"{{{SVG_NS}}}rect").get("id") != "r1"
    ids = [e.get("id") for e in tree.iter() if isinstance(e.tag, str) and e.get("id")]
    assert len(ids) == len(set(ids))


def test_duplicate_with_explicit_new_id_is_referenceable(tool_ws, doc) -> None:
    run_ops(
        doc,
        {"op": "duplicate", "ids": ["g1"], "new_id": "g1_copy"},
        {"op": "move", "ids": ["$0"], "dx": 5, "dy": 0},
    )
    assert _find(_tree(tool_ws, doc), "g1_copy").get("transform") == "translate(5,0)"


def test_duplicate_of_several_targets(tool_ws, doc) -> None:
    before = _ids(tool_ws, doc)
    result = run_ops(doc, {"op": "duplicate", "select": {"id_prefix": "seed"}})
    created = result.outcomes[0].created_ids
    assert len(created) == 2
    assert _ids(tool_ws, doc) == before | set(created)


@pytest.mark.parametrize(
    ("op", "match"),
    [
        ({"op": "duplicate", "ids": ["g1"], "new_id": "r1"}, "id already in use"),
        ({"op": "duplicate", "ids": ["seed1", "seed2"], "new_id": "x"}, "needs exactly one target"),
        ({"op": "duplicate", "ids": ["g1"], "new_id": "bad id"}, "invalid object id"),
    ],
)
def test_duplicate_rejections(tool_ws, doc, op, match) -> None:
    _rejected(tool_ws, doc, op, match=match)


# --- rename ----------------------------------------------------------------------------


def test_rename_rewrites_references(tool_ws, doc) -> None:
    run_ops(doc, {"op": "rename", "ids": ["r1"], "new_id": "rect_renamed"})
    tree = _tree(tool_ws, doc)
    assert _find(tree, "r1") is None and _find(tree, "rect_renamed") is not None
    use = _find(tree, "u1")
    assert (use.get(f"{{{XLINK_NS}}}href") or use.get("href")) == "#rect_renamed"


def test_rename_sets_the_label(tool_ws, doc) -> None:
    run_ops(doc, {"op": "rename", "ids": ["g1"], "label": "My Group"})
    assert _find(_tree(tool_ws, doc), "g1").get(f"{{{INKSCAPE_NS}}}label") == "My Group"


@pytest.mark.parametrize(
    ("op", "match"),
    [
        ({"op": "rename", "ids": ["g1"], "new_id": "r1"}, "id already in use"),
        ({"op": "rename", "ids": ["g1"]}, "at least one of new_id / label"),
        ({"op": "rename", "ids": ["g1"], "new_id": "g1"}, "same as the current id"),
        ({"op": "rename", "ids": ["g1"], "label": "bad\x00"}, "control characters"),
        ({"op": "rename", "ids": ["seed1", "seed2"], "new_id": "x"}, "exactly one target"),
    ],
)
def test_rename_rejections(tool_ws, doc, op, match) -> None:
    _rejected(tool_ws, doc, op, match=match)


# --- delete ----------------------------------------------------------------------------


def test_delete_removes_the_targets_and_reports_them(tool_ws, doc) -> None:
    result = run_ops(doc, _delete(["seed1", "seed2"]), approval_token=TOKEN)
    assert result.changed is True
    assert "'seed1'" in result.summary and "'seed2'" in result.summary
    assert not {"seed1", "seed2"} & _ids(tool_ws, doc)
    assert {"t1", "g1", "r1", "u1"} <= _ids(tool_ws, doc)


def test_delete_is_undone_byte_for_byte(tool_ws, doc) -> None:
    before = _working(tool_ws, doc)
    result = run_ops(doc, _delete(["seed1", "seed2"]), approval_token=TOKEN)
    assert _working(tool_ws, doc) != before
    restore_snapshot(doc, result.snapshot_id)
    assert _working(tool_ws, doc) == before


def test_delete_accepts_a_selector(tool_ws, doc) -> None:
    run_ops(doc, {"op": "delete", "select": {"id_prefix": "seed"}}, approval_token=TOKEN)
    assert not {"seed1", "seed2"} & _ids(tool_ws, doc)


@pytest.mark.parametrize(
    ("ids", "match"),
    [
        (["nope", "also-missing"], "id 'nope' not found"),
        # one absent id fails the WHOLE call: the present one is not deleted either
        (["seed1", "ghost"], "id 'ghost' not found"),
        ([], "'ids' is empty"),
    ],
)
def test_delete_with_missing_or_empty_ids_is_rejected_whole(tool_ws, doc, ids, match) -> None:
    _rejected(tool_ws, doc, _delete(ids), match=match, approval_token=TOKEN)


@pytest.mark.parametrize("token", [None, "", "   "])
def test_delete_without_a_real_token_is_refused_before_any_write(tool_ws, doc, token) -> None:
    _rejected(
        tool_ws,
        doc,
        _delete(["seed1"]),
        match=r"ops\[0\] delete is HIGH risk; re-call with approval_token",
        approval_token=token,
    )
