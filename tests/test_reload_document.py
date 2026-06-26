"""Tool + registry tests (E14-06): `reload_document`.

`reload_document` refreshes a working copy FROM ITS SOURCE under the SAME `doc_id`: it takes a
pre-reload snapshot (so the refresh is reversible), re-resolves + re-validates the source is still
in the sandbox, then re-copies the source over the working copy. A `create_document` doc has no
external source, so its reload restores from the blank seed (`original.svg`).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.server import mcp
from inkscape_mcp.snapshots import list_snapshots, restore_snapshot
from inkscape_mcp.tools.document import create_document, open_document, reload_document

SVG_V1 = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">
  <rect id="r1" x="1" y="1" width="2" height="2"/>
</svg>
"""

SVG_V2 = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10">
  <rect id="r1" x="1" y="1" width="2" height="2"/>
  <circle id="c2" cx="5" cy="5" r="3"/>
</svg>
"""


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    return ws


def test_reload_pulls_updated_source_under_same_doc_id(root: Path) -> None:
    src = root / "a.svg"
    src.write_bytes(SVG_V1)
    opened = open_document(str(src))
    doc_id = opened.doc_id
    assert opened.summary.num_objects == 1

    # The source changes on disk (external edit).
    src.write_bytes(SVG_V2)

    result = reload_document(doc_id)
    assert result.doc_id == doc_id  # SAME id
    # Working copy now reflects the updated source.
    entry = get_registry().get(doc_id)
    working = Path(entry.working_path).read_text(encoding="utf-8")
    assert 'id="c2"' in working
    assert result.summary.num_objects == 2


def test_reload_takes_pre_reload_snapshot_and_is_reversible(root: Path) -> None:
    src = root / "a.svg"
    src.write_bytes(SVG_V1)
    doc_id = open_document(str(src)).doc_id
    entry = get_registry().get(doc_id)
    before = Path(entry.working_path).read_bytes()

    src.write_bytes(SVG_V2)
    result = reload_document(doc_id)
    assert result.pre_reload_snapshot_id
    # The pre-reload snapshot exists in the index.
    snaps = list_snapshots(doc_id)
    assert any(s.snapshot_id == result.pre_reload_snapshot_id for s in snaps)
    # Restoring the pre-reload snapshot undoes the refresh (back to V1 bytes).
    restore_snapshot(doc_id, result.pre_reload_snapshot_id)
    assert Path(entry.working_path).read_bytes() == before


def test_reload_discards_working_edits(root: Path) -> None:
    """A reload returns the working copy to its source, discarding intervening working edits."""
    src = root / "a.svg"
    src.write_bytes(SVG_V1)
    doc_id = open_document(str(src)).doc_id
    entry = get_registry().get(doc_id)
    # Simulate an in-place working edit.
    Path(entry.working_path).write_bytes(SVG_V2)

    reload_document(doc_id)
    working = Path(entry.working_path).read_text(encoding="utf-8")
    assert 'id="c2"' not in working  # back to V1 (source unchanged)


def test_reload_created_document_restores_from_seed(root: Path) -> None:
    """A create_document doc has no external source; reload restores from the blank seed."""
    doc_id = create_document(width=20, height=20).doc_id
    entry = get_registry().get(doc_id)
    seed = Path(entry.working_path).read_bytes()
    # Mutate the working copy out of band.
    Path(entry.working_path).write_bytes(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" '
        b'viewBox="0 0 20 20"><rect id="x" x="0" y="0" width="2" height="2"/></svg>'
    )
    result = reload_document(doc_id)
    assert result.doc_id == doc_id
    # Restored to the original blank seed.
    assert Path(entry.working_path).read_bytes() == seed


def test_reload_unknown_doc_id(root: Path) -> None:
    with pytest.raises(ToolError) as exc:
        reload_document("d_nope")
    assert "not found" in str(exc.value)


def test_reload_rejects_source_moved_outside_sandbox(root: Path) -> None:
    """If the external source no longer resolves inside the sandbox it falls back to original.svg.

    The registry re-resolves the source only when it still EXISTS inside the root; a source removed
    from the workspace cannot redirect the reload outside the sandbox — the reload then restores
    from the immutable in-sandbox baseline (original.svg) rather than failing.
    """
    src = root / "a.svg"
    src.write_bytes(SVG_V1)
    doc_id = open_document(str(src)).doc_id
    # Remove the external source entirely.
    src.unlink()
    # Reload still succeeds (restores from the in-sandbox original.svg baseline).
    result = reload_document(doc_id)
    entry = get_registry().get(doc_id)
    working = Path(entry.working_path).read_text(encoding="utf-8")
    assert 'id="r1"' in working
    assert result.doc_id == doc_id


def test_reload_document_registered(root: Path) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "reload_document" in names
