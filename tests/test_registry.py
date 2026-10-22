"""Registry tests (workspace-model.md §5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inkscape_mcp.config import get_settings
from inkscape_mcp.registry import Registry
from inkscape_mcp.workspace import sandbox

SVG_BODY = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'


def test_open_document_creates_workspace(tool_ws: Path) -> None:
    root = tool_ws
    src = root / "logo.svg"
    src.write_bytes(SVG_BODY)

    reg = Registry(get_settings())
    entry = reg.open_document(str(src))

    assert entry.doc_id.startswith("d_")
    original = Path(entry.original_path)
    working = Path(entry.working_path)
    assert original == sandbox.original_copy(root, entry.doc_id)
    assert working == sandbox.working_copy(root, entry.doc_id)
    assert original.read_bytes() == SVG_BODY
    assert working.read_bytes() == SVG_BODY
    assert src.read_bytes() == SVG_BODY  # the true source file is untouched


def test_registry_json_relative_source(tool_ws: Path) -> None:
    root = tool_ws
    subdir = root / "designs"
    subdir.mkdir()
    src = subdir / "poster.svg"
    src.write_bytes(SVG_BODY)

    reg = Registry(get_settings())
    entry = reg.open_document(str(src))

    reg_path = root / sandbox.STATE_DIR / "registry.json"
    assert reg_path.is_file()
    data = json.loads(reg_path.read_text())
    docs = data["documents"]
    assert len(docs) == 1
    stored = docs[0]["source_path"]
    # Stored RELATIVE to the root, never an absolute host path.
    assert stored == "designs/poster.svg"
    assert not Path(stored).is_absolute()
    assert str(root) not in reg_path.read_text()
    assert entry.source_path == "designs/poster.svg"


def test_get_unknown_id_raises_keyerror(tool_ws: Path) -> None:
    reg = Registry(get_settings())
    with pytest.raises(KeyError):
        reg.get("d_deadbeef")


def test_list_documents(tool_ws: Path) -> None:
    root = tool_ws
    src = root / "a.svg"
    src.write_bytes(SVG_BODY)
    reg = Registry(get_settings())
    e = reg.open_document(str(src))
    assert [d.doc_id for d in reg.list_documents()] == [e.doc_id]
