"""`workspace.stat` engine: size + sha256 of sandboxed artifacts, aggregated over a set."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.workspace.paths import SandboxViolation
from inkscape_mcp.workspace.stat import stat_many, stat_one


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(root))
    get_settings.cache_clear()
    return root


def test_stat_one_reports_relative_path_size_and_digest(ws: Path) -> None:
    (ws / "a.bin").write_bytes(b"hello")
    stat = stat_one("a.bin")
    assert stat.path == "a.bin"
    assert stat.bytes == 5
    assert stat.sha256 == hashlib.sha256(b"hello").hexdigest()


def test_stat_many_sums_bytes_and_preserves_order(ws: Path) -> None:
    (ws / "a.bin").write_bytes(b"12")
    (ws / "b.bin").write_bytes(b"345")
    result = stat_many(["b.bin", "a.bin"])
    assert [a.path for a in result.artifacts] == ["b.bin", "a.bin"]
    assert result.total_bytes == 5
    assert result.count == 2


def test_stat_many_rejects_a_path_outside_the_workspace(ws: Path) -> None:
    (ws / "a.bin").write_bytes(b"x")
    with pytest.raises(SandboxViolation):
        stat_many(["a.bin", "../escape.bin"])


def test_stat_not_found_names_the_root_rule(ws: Path) -> None:
    (ws / "a").mkdir()
    (ws / "a" / "f.svg").write_text("<svg/>")
    assert stat_one(str(ws / "a" / "f.svg")).path == "a/f.svg"  # absolute in-root works
    for raw in ("f.svg", str(ws / "f.svg")):
        with pytest.raises(SandboxViolation) as exc:
            stat_one(raw)
        assert "under the workspace root" in str(exc.value)
        assert "'f.svg'" in str(exc.value)
        assert str(ws) not in str(exc.value)
