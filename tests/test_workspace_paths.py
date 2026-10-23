"""Path-safety tests (workspace-model.md §3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, Settings, get_settings
from inkscape_mcp.workspace.paths import (
    SandboxViolation,
    _safe_mkdir_chain,
    anchor_to_root,
    ensure_parent_within_sandbox,
    in_state_dir,
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


# --- write path: final-component symlink guard (SV5) + read-path oracle --------------------


def test_new_file_in_root_resolves(root_settings: Settings) -> None:
    root = root_settings.workspace_roots[0]
    out = resolve_write_path(str(root / "out.svg"), root_settings)
    assert out == root.resolve() / "out.svg"
    assert not out.exists()


def test_existing_regular_file_resolves(root_settings: Settings) -> None:
    target = root_settings.workspace_roots[0] / "existing.svg"
    target.write_bytes(b"<svg/>")
    assert resolve_write_path(str(target), root_settings) == target.resolve()


def test_symlinked_final_component_outside_rejected(
    root_settings: Settings, tmp_path: Path
) -> None:
    """A symlink AT the final name pointing outside the workspace is rejected (host-path-free)."""
    root = root_settings.workspace_roots[0]
    escape_target = tmp_path / "escape_target.svg"  # outside the root
    link = root / "evil_link.svg"
    link.symlink_to(escape_target)
    with pytest.raises(SandboxViolation) as exc:
        resolve_write_path(str(link), root_settings)
    assert str(exc.value) == "path rejected: outside workspace"


def test_symlinked_final_component_inside_allowed(root_settings: Settings) -> None:
    """A symlink whose real target is still inside the sandbox is permitted (containment holds)."""
    root = root_settings.workspace_roots[0]
    real = root / "real.svg"
    real.write_bytes(b"<svg/>")
    link = root / "link.svg"
    link.symlink_to(real)
    # Returned path is the constructed final path (parent resolved + final name).
    assert resolve_write_path(str(link), root_settings) == root.resolve() / "link.svg"


def test_dangling_symlink_outside_rejected(root_settings: Settings, tmp_path: Path) -> None:
    """A dangling (non-existent target) symlink pointing outside is still rejected."""
    link = root_settings.workspace_roots[0] / "dangling.svg"
    link.symlink_to(tmp_path / "nope" / "gone.svg")
    with pytest.raises(SandboxViolation) as exc:
        resolve_write_path(str(link), root_settings)
    assert str(exc.value) == "path rejected: outside workspace"


def test_parent_outside_rejected(root_settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation) as exc:
        resolve_write_path(str(tmp_path / "out.svg"), root_settings)
    assert str(exc.value) == "path rejected: outside workspace"


def test_read_path_follows_symlink_and_checks_containment(
    root_settings: Settings, tmp_path: Path
) -> None:
    """resolve_read_path follows symlinks fully; an out-of-sandbox target is rejected."""
    outside = tmp_path / "outside.svg"
    outside.write_bytes(b"<svg/>")
    link = root_settings.workspace_roots[0] / "read_link.svg"
    link.symlink_to(outside)
    with pytest.raises(SandboxViolation) as exc:
        resolve_read_path(str(link), root_settings)
    assert str(exc.value) == "path rejected: outside workspace"


def test_read_path_missing_inside_reports_not_found(root_settings: Settings) -> None:
    """An in-sandbox path that does not exist reports the in-workspace `file not found`."""
    with pytest.raises(SandboxViolation) as exc:
        resolve_read_path(str(root_settings.workspace_roots[0] / "missing.svg"), root_settings)
    assert str(exc.value) == "path rejected: file not found"


def test_read_path_missing_outside_reports_outside_not_oracle(
    root_settings: Settings, tmp_path: Path
) -> None:
    """A NONEXISTENT out-of-sandbox path reports `outside workspace`, not `file not found`.

    Otherwise missing-vs-existing outside paths would leak a host existence oracle (sec.12).
    """
    with pytest.raises(SandboxViolation) as exc:
        resolve_read_path(str(tmp_path / "does_not_exist.svg"), root_settings)
    assert str(exc.value) == "path rejected: outside workspace"


# --- sibling-prefix containment, anchoring, parent-dir creation -------------------------------


def test_sibling_prefix_root_not_contained(root_settings: Settings) -> None:
    """`/x/ws` must NOT contain `/x/ws2/f` (the classic `startswith` bug)."""
    root = root_settings.workspace_roots[0]
    sibling = root.parent / (root.name + "2")
    sibling.mkdir()
    f = sibling / "f.svg"
    f.write_bytes(b"<svg/>")
    assert not is_contained(f, [root])
    assert owning_root(f, [root]) is None
    for resolve in (resolve_read_path, resolve_write_path):
        with pytest.raises(SandboxViolation, match="outside workspace"):
            resolve(str(f), root_settings)


def test_anchor_relative_dotdot_joins_root_then_refused(
    root_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative `../x` joins against root[0] (not the CWD) and the choke point refuses it."""
    root = root_settings.workspace_roots[0]
    monkeypatch.chdir(root)  # CWD-relative join would land at tmp_path/x — must not matter
    (tmp_path / "x").write_bytes(b"<svg/>")
    anchored = anchor_to_root("../x", root_settings)
    assert anchored == str(root / ".." / "x")
    with pytest.raises(SandboxViolation, match="outside workspace"):
        resolve_read_path(anchored, root_settings)
    assert anchor_to_root("sub/x", root_settings) == str(root / "sub" / "x")


def test_ensure_parent_refuses_dotdot_creates_nothing(root_settings: Settings) -> None:
    root = root_settings.workspace_roots[0]
    with pytest.raises(SandboxViolation, match="outside workspace"):
        ensure_parent_within_sandbox(root / "a" / ".." / "b" / "f.svg", root_settings)
    assert list(root.iterdir()) == []


def test_ensure_parent_creates_missing_tail(root_settings: Settings) -> None:
    root = root_settings.workspace_roots[0]
    ensure_parent_within_sandbox(root / "a" / "b" / "f.svg", root_settings)
    assert (root / "a" / "b").is_dir()
    assert not (root / "a" / "b" / "f.svg").exists()


def test_ensure_parent_refuses_symlinked_intermediate_outside(
    root_settings: Settings, tmp_path: Path
) -> None:
    root = root_settings.workspace_roots[0]
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(SandboxViolation) as ei:
        ensure_parent_within_sandbox(root / "link" / "new" / "f.svg", root_settings)
    assert str(ei.value) == "path rejected: outside workspace"
    _assert_no_host_path(ei.value, str(outside))
    assert list(outside.iterdir()) == []


def test_safe_mkdir_chain_never_descends_symlink(root_settings: Settings, tmp_path: Path) -> None:
    """The O_NOFOLLOW descent refuses a symlinked component (e.g. raced in after the check)."""
    root = root_settings.workspace_roots[0].resolve()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(SandboxViolation, match="outside workspace"):
        _safe_mkdir_chain(root, ("link", "new"))
    assert list(outside.iterdir()) == []


def test_in_state_dir_checks_every_nested_root_case_insensitively(tmp_path: Path) -> None:
    ws, sub = tmp_path / "ws", tmp_path / "ws" / "sub"
    assert in_state_dir(sub / ".inkscape-mcp" / "x", [ws, sub])  # not only the first root
    assert in_state_dir(ws / ".INKSCAPE-MCP" / "x", [ws])
    assert not in_state_dir(ws / "sub" / "x.svg", [ws, sub])
