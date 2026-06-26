"""Tool-layer tests: open_document / inspect_document on the shared mcp app."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.registry import reset_registry
from inkscape_mcp.server import mcp
from inkscape_mcp.tools.document import inspect_document, open_document

SVG = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
     width="10" height="10" viewBox="0 0 10 10">
  <g id="l1" inkscape:groupmode="layer" inkscape:label="L1">
    <rect id="r1" x="2" y="3" width="4" height="5" style="fill:#ff0000"/>
  </g>
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


def test_open_document_returns_id_and_summary(root: Path) -> None:
    src = root / "a.svg"
    src.write_bytes(SVG)
    result = open_document(str(src))
    assert result.doc_id.startswith("d_")
    assert result.summary.root_tag == "svg"
    assert result.summary.num_layers == 1


def test_inspect_document_aggregate(root: Path) -> None:
    src = root / "a.svg"
    src.write_bytes(SVG)
    opened = open_document(str(src))
    result = inspect_document(opened.doc_id)
    assert result.summary.doc_id == opened.doc_id
    assert result.tree.root.tag == "svg"
    assert any(layer.label == "L1" for layer in result.layers.layers)
    assert "#ff0000" in result.styles.colors


def test_inspect_document_tree_carries_bbox(root: Path) -> None:
    """Per-element bbox reaches an agent through the inspect_document tree (no disk read)."""
    src = root / "a.svg"
    src.write_bytes(SVG)
    opened = open_document(str(src))
    result = inspect_document(opened.doc_id)

    found = {}

    def walk(node) -> None:
        if node.id:
            found[node.id] = node
        for child in node.children:
            walk(child)

    walk(result.tree.root)
    rect = found["r1"]
    assert rect.bbox is not None
    assert (rect.bbox.x, rect.bbox.y, rect.bbox.width, rect.bbox.height) == (2.0, 3.0, 4.0, 5.0)


def test_inspect_unknown_id_raises_toolerror(root: Path) -> None:
    with pytest.raises(ToolError) as exc:
        inspect_document("d_nope")
    assert "not found" in str(exc.value)


def test_open_outside_root_raises_no_host_path(tmp_path: Path, root: Path) -> None:
    outside = tmp_path / "outside.svg"
    outside.write_bytes(SVG)
    with pytest.raises(ToolError) as exc:
        open_document(str(outside))
    msg = str(exc.value)
    assert "rejected" in msg
    # No host path leaks into the client-facing message.
    assert str(tmp_path) not in msg
    assert str(outside) not in msg


def test_tools_registered_on_mcp(root: Path) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert "open_document" in names
    assert "inspect_document" in names


# ---: open_document accepts a workspace-relative path (anchored to the root) --------


def test_open_relative_path_anchors_to_workspace_root(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace-relative path opens (anchored to the first root), NOT the process CWD.

    Reproduces the reported failure: `open_document("e2e/brandmark.svg")` succeeded only with an
    absolute prefix. Runs from a CWD that is NOT the workspace root so a CWD-anchored resolution
    would still fail — proving the anchor is the workspace root.
    """
    subdir = root / "e2e"
    subdir.mkdir()
    (subdir / "brandmark.svg").write_bytes(SVG)
    other_cwd = root.parent / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    result = open_document("e2e/brandmark.svg")
    assert result.doc_id.startswith("d_")
    assert result.summary.root_tag == "svg"


def test_open_relative_escape_still_rejected(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative `../escape` resolving OUTSIDE the workspace is still rejected after anchoring."""
    outside = root.parent / "escape.svg"
    outside.write_bytes(SVG)
    monkeypatch.chdir(root)
    with pytest.raises(ToolError) as exc:
        open_document("../escape.svg")
    assert str(exc.value) == "path rejected: outside workspace"
    # No host path leaks into the client-facing message.
    assert str(root) not in str(exc.value)
    assert str(outside) not in str(exc.value)


def test_open_absolute_outside_still_rejected(root: Path) -> None:
    """An ABSOLUTE path outside the workspace is still rejected (anchor leaves absolutes alone)."""
    outside = root.parent / "abs-outside.svg"
    outside.write_bytes(SVG)
    with pytest.raises(ToolError) as exc:
        open_document(str(outside))
    assert str(exc.value) == "path rejected: outside workspace"
    assert str(outside) not in str(exc.value)


def test_open_symlink_to_outside_still_rejected(root: Path) -> None:
    """A workspace-relative symlink whose target leaves the sandbox is rejected (symlink guard)."""
    outside = root.parent / "secret.svg"
    outside.write_bytes(SVG)
    # An in-workspace name that is actually a symlink pointing OUTSIDE the workspace root.
    (root / "link.svg").symlink_to(outside)
    with pytest.raises(ToolError) as exc:
        open_document("link.svg")
    assert str(exc.value) == "path rejected: outside workspace"
    assert str(outside) not in str(exc.value)
