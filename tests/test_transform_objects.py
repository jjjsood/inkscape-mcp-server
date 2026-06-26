"""Constrained computed-edit path tests (E20-02): ``transform_objects``.

Proves the binding acceptance criteria:

1. selector reuses the `find_objects` predicate engine (a paint/tag selector resolves right ids);
2. exactly ONE typed op per call, drawn from the `apply_edits` member set (rejected ops raise);
3. `dry_run` is the DEFAULT and returns matched ids + the projected plan with ZERO mutation;
4. `max_matches` REJECTS an over-broad selector BEFORE any mutation;
5. ONE snapshot + ONE Operation Record for the whole transform; a mid-batch failure rolls back
   atomically; a single `restore_snapshot` reverts it;
6. effective risk = the op's class — a `delete_object` op forces the `approval_token` gate;
7. surfaced via `intents` / `how_do_i`.

Hermetic: the pipeline's preview rendering is monkeypatched so no Inkscape is invoked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from lxml import etree

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.intents import INTENT_MAP, match_intents
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render.cli import RenderResult
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.tools.transform_objects import transform_objects
from inkscape_mcp.workspace import sandbox

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
    b'<rect id="r1" x="0" y="0" width="10" height="10" fill="#3366cc"/>'
    b'<rect id="r2" x="20" y="0" width="10" height="10" fill="#3366cc"/>'
    b'<rect id="r3" x="40" y="0" width="10" height="10" fill="#ff0000"/>'
    b'<text id="t1" x="0" y="50">hello</text>'
    b"</svg>"
)


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
def doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    monkeypatch.setattr("inkscape_mcp.edit.pipeline.render_preview", _fake_render_preview)
    src = ws / "logo.svg"
    src.write_bytes(SVG)
    entry = get_registry().open_document(str(src))
    return entry.doc_id, Path(entry.root)


def _working_root(root: Path, doc_id: str) -> etree._Element:
    return etree.fromstring(sandbox.working_copy(root, doc_id).read_bytes())


def _find(root: etree._Element, object_id: str) -> etree._Element | None:
    for elem in root.iter():
        if isinstance(elem.tag, str) and elem.get("id") == object_id:
            return elem
    return None


def _paint(node: etree._Element | None) -> str:
    """Concatenated style + fill of a node, for a substring paint assertion."""
    assert node is not None
    return (node.get("style") or "") + (node.get("fill") or "")


def _snapshot_count(root: Path, doc_id: str) -> int:
    index = sandbox.snapshots_index(root, doc_id)
    if not index.is_file():
        return 0
    return len(json.loads(index.read_text()))


# --- (1) selector reuse + (3) dry_run default zero-mutation -------------------


def test_dry_run_default_resolves_selector_and_mutates_nothing(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()

    result = transform_objects(
        doc_id,
        {"tag": "rect", "fill": "#3366cc"},
        {"op": "set_fill", "color": "#00ff00"},
    )

    # Selector reuse: only the two blue rects, NOT the red rect or the text.
    assert result.dry_run is True
    assert set(result.matched_ids) == {"r1", "r2"}
    assert result.match_count == 2
    # The projected plan is the single fanned-out edit covering both ids.
    assert [p.op for p in result.plan] == ["set_fill"]
    assert set(result.plan[0].object_ids) == {"r1", "r2"}
    assert result.applied is False and result.operation_id == ""
    # Nothing written.
    assert sandbox.working_copy(root, doc_id).read_bytes() == before
    assert _snapshot_count(root, doc_id) == 0


# --- (1)+(5) real run applies to every match, one snapshot/record ------------


def test_real_run_applies_to_every_match_with_one_snapshot(doc: tuple[str, Path]) -> None:
    doc_id, root = doc

    result = transform_objects(
        doc_id,
        {"tag": "rect", "fill": "#3366cc"},
        {"op": "set_fill", "color": "#00ff00"},
        dry_run=False,
    )

    assert result.applied is True and result.changed is True
    assert result.operation_id and result.snapshot_id
    tree = _working_root(root, doc_id)
    assert "#00ff00" in _paint(_find(tree, "r1"))
    assert "#00ff00" in _paint(_find(tree, "r2"))
    # The unmatched red rect is untouched.
    assert "#00ff00" not in _paint(_find(tree, "r3"))
    # Exactly ONE snapshot for the whole transform.
    assert _snapshot_count(root, doc_id) == 1

    # A single restore_snapshot reverts the entire transform.
    restore_snapshot(doc_id, result.snapshot_id)
    reverted = _working_root(root, doc_id)
    for oid in ("r1", "r2"):
        assert "#00ff00" not in _paint(_find(reverted, oid))


# --- (2) exactly one op, from the accepted member set ------------------------


def test_rejected_op_outside_accepted_set(doc: tuple[str, Path]) -> None:
    doc_id, _root = doc
    # `replace_color` is document-wide — not a per-match op; the discriminated union rejects it.
    with pytest.raises(ToolError):
        transform_objects(
            doc_id,
            {"tag": "rect"},
            {"op": "replace_color", "from_color": "#3366cc", "to_color": "#000000"},
        )
    # A create op is likewise not accepted.
    with pytest.raises(ToolError):
        transform_objects(
            doc_id,
            {"tag": "rect"},
            {"op": "create_rect", "x": 0, "y": 0, "width": 5, "height": 5},
        )


# --- (4) max_matches rejects an over-broad selector before mutation ----------


def test_max_matches_rejects_before_mutation(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    with pytest.raises(ToolError) as exc:
        transform_objects(
            doc_id,
            {"tag": "rect", "fill": "#3366cc"},
            {"op": "set_fill", "color": "#00ff00"},
            dry_run=False,
            max_matches=1,
        )
    assert "max_matches" in str(exc.value)
    # No mutation, no snapshot.
    assert sandbox.working_copy(root, doc_id).read_bytes() == before
    assert _snapshot_count(root, doc_id) == 0


# --- (5) atomic rollback on a mid-batch failure ------------------------------


def test_atomic_rollback_on_mid_batch_failure(
    doc: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()

    # A move op fans out to N single-target edits. Make the SECOND edit's engine builder raise so
    # the batch aborts mid-stream; the kernel writes only after the whole mutate returns, so the
    # document must stay byte-identical (all-or-nothing).
    import inkscape_mcp.edit.batch as batch_mod

    real_move = batch_mod.transform_engine.move
    calls = {"n": 0}

    def flaky_move(tree: etree._ElementTree, object_id: str, dx: float, dy: float) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            from inkscape_mcp.edit.dom import EditError

            raise EditError("boom")
        return real_move(tree, object_id, dx, dy)

    monkeypatch.setattr(batch_mod.transform_engine, "move", flaky_move)

    with pytest.raises(ToolError):
        transform_objects(
            doc_id,
            {"tag": "rect", "fill": "#3366cc"},
            {"op": "move_object", "dx": 5, "dy": 5},
            dry_run=False,
        )
    # All-or-nothing: nothing persisted.
    assert sandbox.working_copy(root, doc_id).read_bytes() == before
    assert _snapshot_count(root, doc_id) == 0


# --- (6) high op (delete) forces the approval_token gate ----------------------


def test_delete_op_requires_approval_token(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()

    # A dry run reports the HIGH risk without needing a token.
    plan = transform_objects(doc_id, {"tag": "rect"}, {"op": "delete_object"})
    assert plan.risk_class == "high"
    assert sandbox.working_copy(root, doc_id).read_bytes() == before

    # A real run with NO token is refused by the policy gate.
    with pytest.raises(ToolError):
        transform_objects(doc_id, {"tag": "rect"}, {"op": "delete_object"}, dry_run=False)
    assert sandbox.working_copy(root, doc_id).read_bytes() == before
    assert _snapshot_count(root, doc_id) == 0

    # With a token the delete applies and is reversible.
    result = transform_objects(
        doc_id,
        {"tag": "rect"},
        {"op": "delete_object"},
        dry_run=False,
        approval_token="ok",
    )
    assert result.applied is True
    tree = _working_root(root, doc_id)
    assert _find(tree, "r1") is None and _find(tree, "r3") is None
    assert _find(tree, "t1") is not None  # text was not matched


# --- (3) empty match on a real run is a clean no-op --------------------------


def test_empty_match_real_run_is_noop(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    result = transform_objects(
        doc_id,
        {"id_prefix": "does-not-exist"},
        {"op": "set_fill", "color": "#00ff00"},
        dry_run=False,
    )
    assert result.match_count == 0
    assert result.applied is False and result.changed is False
    assert result.plan == []
    assert _snapshot_count(root, doc_id) == 0


# --- (7) intent surfacing ----------------------------------------------------


def test_intent_surfaces_transform_objects() -> None:
    assert any("transform_objects" in e.tools for e in INTENT_MAP)
    matches = match_intents("recolour all the blue rectangles")
    assert any("transform_objects" in m.tools for m in matches)
