"""Unit tests for the sandbox path choke point (workspace/paths.py).

Focus: the FINAL-component symlink guard on the write path (SV5). A pre-existing
symlink at the destination filename must not let a write escape the configured workspace
roots, even though the parent directory is contained.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from inkscape_mcp.config import Settings
from inkscape_mcp.workspace.paths import (
    SandboxViolation,
    resolve_read_path,
    resolve_write_path,
)


def _settings(root: Path) -> Settings:
    """A Settings with a single resolved workspace root (avoids env/cache coupling)."""
    return Settings(workspace_roots=[root.resolve()])


@pytest.fixture
def root(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def test_new_file_in_root_resolves(root: Path) -> None:
    out = resolve_write_path(str(root / "out.svg"), settings=_settings(root))
    assert out == (root.resolve() / "out.svg")


def test_existing_regular_file_resolves(root: Path) -> None:
    target = root / "existing.svg"
    target.write_bytes(b"<svg/>")
    out = resolve_write_path(str(target), settings=_settings(root))
    assert out == target.resolve()


def test_symlinked_final_component_outside_rejected(root: Path, tmp_path: Path) -> None:
    """A symlink AT the final name pointing outside the workspace is rejected (host-path-free)."""
    escape_target = tmp_path / "escape_target.svg"  # outside the root
    link = root / "evil_link.svg"
    link.symlink_to(escape_target)

    with pytest.raises(SandboxViolation) as exc:
        resolve_write_path(str(link), settings=_settings(root))
    assert str(exc.value) == "path rejected: outside workspace"
    # The public message carries no host filesystem path.
    assert str(escape_target) not in str(exc.value)
    assert str(root) not in str(exc.value)


def test_symlinked_final_component_inside_allowed(root: Path) -> None:
    """A symlink whose real target is still inside the sandbox is permitted (containment holds)."""
    real = root / "real.svg"
    real.write_bytes(b"<svg/>")
    link = root / "link.svg"
    link.symlink_to(real)

    # Permitted (the link's real target is contained); the returned path is the constructed
    # final path (parent resolved + final name), which is itself inside the root.
    out = resolve_write_path(str(link), settings=_settings(root))
    assert out == (root.resolve() / "link.svg")


def test_dangling_symlink_outside_rejected(root: Path, tmp_path: Path) -> None:
    """A dangling (non-existent target) symlink pointing outside is still rejected."""
    link = root / "dangling.svg"
    link.symlink_to(tmp_path / "nope" / "gone.svg")
    with pytest.raises(SandboxViolation) as exc:
        resolve_write_path(str(link), settings=_settings(root))
    assert str(exc.value) == "path rejected: outside workspace"


def test_parent_outside_rejected(root: Path, tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation) as exc:
        resolve_write_path(str(tmp_path / "out.svg"), settings=_settings(root))
    assert str(exc.value) == "path rejected: outside workspace"


def test_read_path_follows_symlink_and_checks_containment(root: Path, tmp_path: Path) -> None:
    """resolve_read_path follows symlinks fully; an out-of-sandbox target is rejected."""
    outside = tmp_path / "outside.svg"
    outside.write_bytes(b"<svg/>")
    link = root / "read_link.svg"
    link.symlink_to(outside)
    with pytest.raises(SandboxViolation) as exc:
        resolve_read_path(str(link), settings=_settings(root))
    assert str(exc.value) == "path rejected: outside workspace"
