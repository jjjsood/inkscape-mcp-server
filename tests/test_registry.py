"""Registry tests (workspace-model.md §5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, Settings, get_settings
from inkscape_mcp.registry import Registry
from inkscape_mcp.workspace import sandbox

SVG_BODY = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'


@pytest.fixture
def settings_with_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(root))
    get_settings.cache_clear()
    return get_settings()


def test_open_document_creates_workspace(settings_with_root: Settings) -> None:
    root = settings_with_root.workspace_roots[0]
    src = root / "logo.svg"
    src.write_bytes(SVG_BODY)

    reg = Registry(settings_with_root)
    entry = reg.open_document(str(src))

    assert entry.doc_id.startswith("d_")
    original = Path(entry.original_path)
    working = Path(entry.working_path)
    assert original == sandbox.original_copy(root, entry.doc_id)
    assert working == sandbox.working_copy(root, entry.doc_id)
    assert original.read_bytes() == SVG_BODY
    assert working.read_bytes() == SVG_BODY


def test_original_source_unchanged(settings_with_root: Settings) -> None:
    root = settings_with_root.workspace_roots[0]
    src = root / "logo.svg"
    src.write_bytes(SVG_BODY)

    reg = Registry(settings_with_root)
    reg.open_document(str(src))
    # The true original file at its source path is byte-identical and untouched.
    assert src.read_bytes() == SVG_BODY


def test_registry_json_relative_source(settings_with_root: Settings) -> None:
    root = settings_with_root.workspace_roots[0]
    subdir = root / "designs"
    subdir.mkdir()
    src = subdir / "poster.svg"
    src.write_bytes(SVG_BODY)

    reg = Registry(settings_with_root)
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


def test_get_unknown_id_raises_keyerror(settings_with_root: Settings) -> None:
    reg = Registry(settings_with_root)
    with pytest.raises(KeyError):
        reg.get("d_deadbeef")


def test_list_documents(settings_with_root: Settings) -> None:
    root = settings_with_root.workspace_roots[0]
    src = root / "a.svg"
    src.write_bytes(SVG_BODY)
    reg = Registry(settings_with_root)
    e = reg.open_document(str(src))
    assert [d.doc_id for d in reg.list_documents()] == [e.doc_id]
