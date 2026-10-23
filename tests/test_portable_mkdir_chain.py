"""Portable (no-`dir_fd`) mkdir-chain fallback used on Windows.

`_safe_mkdir_chain` relies on `os.O_DIRECTORY` / `os.O_NOFOLLOW` / `dir_fd`, none of which exist
on Windows, so there every export / save / sync that creates a directory crashed. These tests force
the fallback on any OS so it stays covered by the Linux/macOS CI legs too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from inkscape_mcp.workspace import paths as ws_paths
from inkscape_mcp.workspace.paths import SandboxViolation, portable_mkdir_chain


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:  # Windows without Developer Mode / SeCreateSymbolicLinkPrivilege
        pytest.skip(f"cannot create symlinks here: {exc}")


def test_creates_nested_chain(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    portable_mkdir_chain(base, ("a", "b", "c"), "dest")
    assert (base / "a" / "b" / "c").is_dir()


def test_existing_components_are_reused(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    (base / "a").mkdir()
    portable_mkdir_chain(base, ("a", "b"), "dest")
    assert (base / "a" / "b").is_dir()


@pytest.mark.parametrize("bad", ["", ".", ".."])
def test_unsafe_component_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(SandboxViolation) as exc:
        portable_mkdir_chain(tmp_path.resolve(), ("ok", bad, "x"), "dest")
    assert "invalid filename" in str(exc.value)


def test_symlinked_component_rejected_and_nothing_created_outside(tmp_path: Path) -> None:
    base = (tmp_path / "ws").resolve()
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(base / "evil", outside)
    with pytest.raises(SandboxViolation) as exc:
        portable_mkdir_chain(base, ("evil", "sub"), "out_dir")
    assert "outside workspace" in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    assert not (outside / "sub").exists()


def test_file_in_the_way_rejected(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    (base / "a").write_text("not a dir")
    with pytest.raises(SandboxViolation):
        portable_mkdir_chain(base, ("a", "b"), "dest")


def test_safe_mkdir_chain_uses_fallback_without_dir_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ws_paths, "HAS_DIR_FD_MKDIR", False)
    base = tmp_path.resolve()
    ws_paths._safe_mkdir_chain(base, ("x", "y"))
    assert (base / "x" / "y").is_dir()


def test_mkdir_error_does_not_leak_host_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Access is denied", str(self))

    monkeypatch.setattr(Path, "mkdir", deny)
    with pytest.raises(SandboxViolation) as exc:
        portable_mkdir_chain(tmp_path.resolve(), ("a",), "dest")
    assert "could not resolve path" in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    assert exc.value.detail is not None and "could not be created" in exc.value.detail
