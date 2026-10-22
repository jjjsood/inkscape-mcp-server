"""Tests for the generated LLM manifest: ``llms.txt`` + ``llms-full.txt``.

The two files are GENERATED from the live MCP registry by ``scripts/gen_llms_txt.py`` (not
hand-maintained). These tests prove the generator covers the WHOLE surface and that the committed
copies are up to date, so the manifest can never silently drift from the real tool/prompt/resource
set the way a hand-edited llms.txt would.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from inkscape_mcp.server import mcp, register_tools

# Register the full surface once so the live counts reflect every primitive (idempotent).
register_tools()

#: Server project root and the two committed generated files.
_SERVER_ROOT = Path(__file__).resolve().parent.parent
_LLMS_TXT = _SERVER_ROOT / "llms.txt"
_LLMS_FULL_TXT = _SERVER_ROOT / "llms-full.txt"


def _load_generator() -> Any:
    """Import ``scripts/gen_llms_txt.py`` as a module (it lives outside the package)."""
    path = _SERVER_ROOT / "scripts" / "gen_llms_txt.py"
    spec = importlib.util.spec_from_file_location("gen_llms_txt", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_llms_txt"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen() -> Any:
    return _load_generator()


@pytest.fixture(scope="module")
def out(gen: Any, tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """One fresh regeneration shared by every test in the module."""
    return gen.generate(dest=tmp_path_factory.mktemp("llms"))


def _live_tool_names() -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def _live_counts() -> tuple[int, int, int]:
    tools = asyncio.run(mcp.list_tools())
    prompts = asyncio.run(mcp.list_prompts())
    resources = asyncio.run(mcp.list_resources())
    templates = asyncio.run(mcp.list_resource_templates())
    return len(tools), len(prompts), len(resources) + len(templates)


def test_every_tool_appears_in_full_manifest(out: dict[str, str]) -> None:
    full = out["llms-full.txt"]
    missing = sorted(n for n in _live_tool_names() if f"#### {n}\n" not in full)
    assert not missing, f"tools missing from llms-full.txt: {missing}"


def test_manifest_tool_count_matches_live_surface(out: dict[str, str]) -> None:
    """The tool count stated in the manifest equals the live tool count (drift guard)."""
    index = out["llms.txt"]
    n_tools, n_prompts, n_resources = _live_counts()
    assert f"Surface: {n_tools} tools, {n_prompts} prompts, {n_resources} resources." in index


def test_every_tool_listed_in_index(out: dict[str, str]) -> None:
    index = out["llms.txt"]
    # Each tool appears once in the grouped one-line list: "- <name> [risk: ...] — ...".
    missing = sorted(n for n in _live_tool_names() if f"- {n} [risk:" not in index)
    assert not missing, f"tools missing from llms.txt index: {missing}"


def test_risk_classes_present(gen: Any, out: dict[str, str]) -> None:
    """The manifest tags risk classes, and every tag is a canonical risk vocabulary token."""
    full = out["llms-full.txt"]
    n_tools = _live_counts()[0]
    vocab = set(gen._RISK_VOCAB)
    # The per-tool risk field is an exact ``Risk class: <canonical token>`` line at column 0. (Some
    # prompt/resource DESCRIPTIONS also contain the phrase "Risk class:" with trailing prose; those
    # are excluded by requiring the whole line to be exactly the field.)
    risk_tokens = [
        ln.removeprefix("Risk class: ")
        for ln in full.splitlines()
        if ln.startswith("Risk class: ") and ln.removeprefix("Risk class: ") in vocab
    ]
    assert len(risk_tokens) == n_tools, "expected one canonical 'Risk class:' field per tool"


def test_committed_files_are_up_to_date(out: dict[str, str]) -> None:
    """The committed llms.txt / llms-full.txt match a fresh regeneration (drift guard).

    If this fails the surface changed without regenerating the manifest — re-run the generator.
    """
    for name, committed_path in (("llms.txt", _LLMS_TXT), ("llms-full.txt", _LLMS_FULL_TXT)):
        assert committed_path.is_file(), f"{name} is not committed at {committed_path}"
        committed = committed_path.read_text(encoding="utf-8")
        assert committed == out[name], (
            f"{name} is STALE — regenerate it with:\n"
            f"    uv run python scripts/gen_llms_txt.py\n"
            f"then commit the updated {name}."
        )


def test_every_edit_op_is_documented_in_both_files(out: dict[str, str]) -> None:
    """The op reference comes from the op union, so a new op shows up without hand edits."""
    from inkscape_mcp.edit.op_docs import op_models

    missing_full = sorted(op for op in op_models() if f"#### {op}\n" not in out["llms-full.txt"])
    assert not missing_full, f"ops missing from llms-full.txt: {missing_full}"
    ops_section = out["llms.txt"].split("## Edit ops", 1)[1]
    index_ops = {
        op.strip().rstrip("*")
        for line in ops_section.splitlines()
        if line.startswith("- ")
        for op in line.split(": ", 1)[1].split(",")
    }
    assert set(op_models()) <= index_ops
    assert "Starred ops are HIGH risk" in out["llms.txt"] and "delete*" in out["llms.txt"]
