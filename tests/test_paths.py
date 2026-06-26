"""Path-safety tests (workspace-model.md §3)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, Settings, get_settings
from inkscape_mcp.workspace.paths import (
    SandboxViolation,
    is_contained,
    owning_root,
    resolve_read_path,
    resolve_write_path,
)


@pytest.fixture
def root_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(root))
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.workspace_roots, "root should be usable"
    return settings


def _assert_no_host_path(exc: SandboxViolation, *paths: str) -> None:
    msg = str(exc)
    for p in paths:
        assert p not in msg, f"public message leaked host path: {msg!r}"


def test_containment_accepts_inside_root(root_settings: Settings) -> None:
    root = root_settings.workspace_roots[0]
    inside = root / "a" / "b"
    assert is_contained(inside, [root])
    assert owning_root(inside, [root]) == root


def test_resolve_read_path_inside_root(root_settings: Settings) -> None:
    root = root_settings.workspace_roots[0]
    f = root / "logo.svg"
    f.write_text("<svg/>")
    resolved = resolve_read_path(str(f), root_settings)
    assert resolved == f.resolve()


def test_reject_dotdot_escape(root_settings: Settings) -> None:
    root = root_settings.workspace_roots[0]
    escape = root / ".." / ".." / "etc" / "passwd"
    with pytest.raises(SandboxViolation) as ei:
        resolve_read_path(str(escape), root_settings)
    _assert_no_host_path(ei.value, str(root), "/etc/passwd")


def test_reject_empty_path(root_settings: Settings) -> None:
    with pytest.raises(SandboxViolation) as ei:
        resolve_read_path("", root_settings)
    assert "empty" in str(ei.value)


def test_reject_nul_byte(root_settings: Settings) -> None:
    with pytest.raises(SandboxViolation):
        resolve_read_path("foo\x00bar", root_settings)


def test_reject_symlink_target_outside_root(root_settings: Settings, tmp_path: Path) -> None:
    root = root_settings.workspace_roots[0]
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    link = root / "link.svg"
    link.symlink_to(outside)
    with pytest.raises(SandboxViolation) as ei:
        resolve_read_path(str(link), root_settings)
    _assert_no_host_path(ei.value, str(outside))


def test_write_path_missing_final_allowed(root_settings: Settings) -> None:
    root = root_settings.workspace_roots[0]
    target = root / "new-file.svg"  # final component does not exist
    resolved = resolve_write_path(str(target), root_settings)
    assert resolved == root.resolve() / "new-file.svg"
    assert not resolved.exists()


def test_write_path_symlinked_missing_intermediate_rejected(
    root_settings: Settings, tmp_path: Path
) -> None:
    root = root_settings.workspace_roots[0]
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    # A symlinked intermediate directory pointing outside the root.
    linked_dir = root / "subdir"
    linked_dir.symlink_to(outside_dir)
    target = linked_dir / "evil.svg"  # parent resolves outside the root
    with pytest.raises(SandboxViolation) as ei:
        resolve_write_path(str(target), root_settings)
    _assert_no_host_path(ei.value, str(outside_dir))


def test_write_path_missing_intermediate_rejected(root_settings: Settings) -> None:
    root = root_settings.workspace_roots[0]
    target = root / "nope" / "evil.svg"  # intermediate 'nope' does not exist
    with pytest.raises(SandboxViolation):
        resolve_write_path(str(target), root_settings)


def test_no_roots_configured_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_WORKSPACE_ROOTS, raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.workspace_roots == []
    with pytest.raises(SandboxViolation) as ei:
        resolve_read_path("/anything", settings)
    assert "no workspace root" in str(ei.value)


def test_unusable_root_excluded_with_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(missing))
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.workspace_roots == []
    assert settings.root_diagnostics
    assert not missing.exists(), "root must never be auto-created"


def test_no_fallback_to_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, "")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.workspace_roots == []
    assert os.getcwd() not in [str(r) for r in settings.workspace_roots]
