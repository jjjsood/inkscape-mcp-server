"""Limit-enforcement tests (workspace-model.md §4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from inkscape_mcp.config import (
    ENV_MAX_EXPORT_PX,
    ENV_MAX_INPUT_BYTES,
    ENV_MAX_OUTPUT_BYTES,
    get_settings,
)
from inkscape_mcp.workspace.limits import (
    LimitExceeded,
    check_export_dimensions,
    check_input_size,
    check_output_size,
)


def test_oversize_input_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_MAX_INPUT_BYTES, "10")
    get_settings.cache_clear()
    f = tmp_path / "big.svg"
    f.write_bytes(b"x" * 100)
    with pytest.raises(LimitExceeded):
        check_input_size(f, get_settings())


def test_input_within_limit_returns_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_MAX_INPUT_BYTES, "1000")
    get_settings.cache_clear()
    f = tmp_path / "ok.svg"
    f.write_bytes(b"x" * 50)
    assert check_input_size(f, get_settings()) == 50


def test_export_dimension_over_cap_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_MAX_EXPORT_PX, "100")
    get_settings.cache_clear()
    s = get_settings()
    check_export_dimensions(100, 100, s)  # exactly at cap: allowed
    with pytest.raises(LimitExceeded):
        check_export_dimensions(101, 50, s)
    with pytest.raises(LimitExceeded):
        check_export_dimensions(50, 101, s)


def test_output_size_over_cap_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_MAX_OUTPUT_BYTES, "10")
    get_settings.cache_clear()
    f = tmp_path / "out.png"
    f.write_bytes(b"x" * 100)
    with pytest.raises(LimitExceeded):
        check_output_size(f, get_settings())
