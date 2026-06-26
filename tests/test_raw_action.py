"""Gated raw-action tool tests (E6-03 / ADR-003 / ADR-002 / ADR-004 / sec.12).

The `run_raw_action` escape hatch is a thin façade over the E6-02 chain machinery (one typed Action
→ a one-step chain) plus an advanced-mode gate. Hermetic: the Inkscape touch points are
monkeypatched — `render_preview` in the pipeline (before/after frames), `run_inkscape` in the chain
engine (the Action run), and `get_or_build_action_map` so validation consults a synthetic capability
map instead of probing.

Covers the acceptance criteria: refused when advanced mode is disabled (the default); refused for a
non-allowlisted Action even when enabled; refused for an Action absent from the versioned map; HIGH
refused without an approval token on a real run; dry-run reports the resolved argv WITHOUT mutating;
an enabled + allowlisted + approved run mutates through the pipeline (snapshot + Operation Record +
previews, reversible); and the assembled argv is an arg list, never a shell string.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from lxml import etree

from inkscape_mcp.actions import chains as chain_engine
from inkscape_mcp.actions.capability_map import ActionCapabilityMap
from inkscape_mcp.config import (
    ENV_RAW_ACTION_ENABLED,
    ENV_WORKSPACE_ROOTS,
    get_settings,
)
from inkscape_mcp.edit import pipeline
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render.cli import RenderResult
from inkscape_mcp.server import mcp
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.tools.actions import run_raw_action
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.subprocess_exec import ProcessResult

SVG_NS = "http://www.w3.org/2000/svg"

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
    b'<path id="p1" d="M10,10 L50,10 L50,50 L10,50 Z" fill="red"/>'
    b'<path id="p2" d="M30,30 L70,30 L70,70 L30,70 Z" fill="blue"/>'
    b"</svg>"
)

ENGINE_OUTPUT = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
    b'<path id="p1" d="M10,10 L50,10 L50,50 L10,50 Z"/>'
    b"</svg>"
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake"
TOKEN = "approved-by-operator"

#: A synthetic capability map: the allowlisted ops we exercise are present; one allowlisted op is
#: deliberately ABSENT to test graceful degradation (mirrors the E6-02 chain tests).
_MAP = ActionCapabilityMap(
    inkscape_version="1.4.3",
    inkscape_version_tuple=(1, 4, 3),
    actions=["select-by-id", "object-to-path", "path-union", "path-difference"],
    action_count=4,
    probed_at=datetime.now(UTC).isoformat(),
)

_LAST_ARGS: list[str] = []


@pytest.fixture
def enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path, Path]:
    """A workspace + opened doc with advanced mode (raw_action_enabled) turned ON."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    monkeypatch.setenv(ENV_RAW_ACTION_ENABLED, "1")
    get_settings.cache_clear()
    reset_registry()
    src = ws / "shapes.svg"
    src.write_bytes(SVG)
    entry = get_registry().open_document(str(src))
    return entry.doc_id, ws, src


@pytest.fixture
def disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path, Path]:
    """A workspace + opened doc with advanced mode left at its DEFAULT (off)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    monkeypatch.delenv(ENV_RAW_ACTION_ENABLED, raising=False)
    get_settings.cache_clear()
    reset_registry()
    src = ws / "shapes.svg"
    src.write_bytes(SVG)
    entry = get_registry().open_document(str(src))
    return entry.doc_id, ws, src


@pytest.fixture(autouse=True)
def fake_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make chain validation consult the synthetic map (no probe / no disk)."""
    monkeypatch.setattr(chain_engine, "get_or_build_action_map", lambda **kw: _MAP)


@pytest.fixture(autouse=True)
def fake_render(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_render_preview(
        doc_id: str, width_px: int | None = None, settings: object | None = None
    ) -> RenderResult:
        entry = get_registry().get(doc_id)
        root = Path(entry.root)
        preview_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        out = preview_dir / "preview-auto.png"
        out.write_bytes(PNG_BYTES)
        # E11-01 one-location contract: artifact_path is workspace-ROOT-relative (matches engine).
        rel = out.relative_to(root).as_posix()
        return RenderResult(
            doc_id=doc_id,
            artifact_path=rel,
            workspace_relative_path=rel,
            format="png",
            width_px=100,
            height_px=100,
            duration_s=0.01,
        )

    monkeypatch.setattr(pipeline, "render_preview", fake_render_preview)


@pytest.fixture(autouse=True)
def fake_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    _LAST_ARGS.clear()

    def fake_run_inkscape(args: list[str], settings: object | None = None) -> ProcessResult:
        _LAST_ARGS[:] = args
        out = next(
            a[len("--export-filename=") :] for a in args if a.startswith("--export-filename=")
        )
        Path(out).write_bytes(ENGINE_OUTPUT)
        return ProcessResult(
            args=list(args), returncode=0, stdout="", stderr="", duration_s=0.02, timed_out=False
        )

    monkeypatch.setattr(chain_engine, "run_inkscape", fake_run_inkscape)


def _working_root(root: Path, doc_id: str) -> etree._Element:
    return etree.parse(str(sandbox.working_copy(root, doc_id))).getroot()


# --- advanced-mode gate (OFF by default) ------------------------------------


def test_raw_action_refused_when_disabled(disabled: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = disabled
    # Even a dry run of an allowlisted+present Action is refused before anything happens.
    with pytest.raises(ToolError) as exc:
        run_raw_action(doc_id, "object-to-path", dry_run=True)
    assert "raw_action_disabled" in str(exc.value)
    assert _LAST_ARGS == []
    assert not list(sandbox.operations_dir(root, doc_id).glob("op_*.json"))


def test_raw_action_disabled_refuses_before_validation(disabled: tuple[str, Path, Path]) -> None:
    doc_id, _root, _ = disabled
    # The gate is checked FIRST: even a non-allowlisted action surfaces the disabled error, not a
    # validation error — proof nothing is attempted while advanced mode is off.
    with pytest.raises(ToolError) as exc:
        run_raw_action(doc_id, "effect.voronoi", dry_run=True)
    assert "raw_action_disabled" in str(exc.value)


# --- allowlist + versioned map gates (when enabled) -------------------------


def test_non_allowlisted_action_refused(enabled: tuple[str, Path, Path]) -> None:
    doc_id, _root, _ = enabled
    with pytest.raises(ToolError) as exc:
        run_raw_action(doc_id, "effect.voronoi", dry_run=True)
    assert "not_allowlisted" in str(exc.value)


def test_allowlisted_but_absent_action_refused(enabled: tuple[str, Path, Path]) -> None:
    doc_id, _root, _ = enabled
    # `path-intersection` IS in the default allowlist but NOT in the synthetic map ⇒ refused clean.
    with pytest.raises(ToolError) as exc:
        run_raw_action(doc_id, "path-intersection", dry_run=True)
    assert "action_absent" in str(exc.value)


def test_malformed_arg_refused(enabled: tuple[str, Path, Path]) -> None:
    doc_id, _root, _ = enabled
    with pytest.raises(ToolError) as exc:
        run_raw_action(doc_id, "select-by-id", args=["p1;evil"], dry_run=True)
    assert "malformed_arg" in str(exc.value)


def test_comma_joined_id_arg_surfaces_hint(enabled: tuple[str, Path, Path]) -> None:
    # E11-10(e) / S15: a comma-joined multi-id token surfaces the actionable separate-token hint
    # through the raw-action façade too (it shares the chain validator).
    doc_id, _root, _ = enabled
    with pytest.raises(ToolError) as exc:
        run_raw_action(doc_id, "select-by-id", args=["p_a,p_b"], dry_run=True)
    text = str(exc.value)
    assert "malformed_arg" in text
    assert "HINT" in text
    assert "separate arg token" in text.lower()


# --- dry-run (default; no mutation) -----------------------------------------


def test_dry_run_is_default_and_does_not_mutate(enabled: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = enabled
    before = sandbox.working_copy(root, doc_id).read_bytes()
    # No dry_run arg ⇒ defaults to dry-run (no approval needed, no mutation).
    result = run_raw_action(doc_id, "select-by-id", args=["p1", "p2"])
    assert result.dry_run is True
    assert result.changed is False
    assert result.operation_id is None
    assert result.snapshot_id is None
    assert result.plan.actions_argument == "select-by-id:p1,p2"
    # argv preview is an arg list, not a shell string.
    assert any(a.startswith("--actions=") for a in result.plan.argv_preview)
    # No engine invocation, no write, no operation record.
    assert _LAST_ARGS == []
    assert sandbox.working_copy(root, doc_id).read_bytes() == before
    assert not list(sandbox.operations_dir(root, doc_id).glob("op_*.json"))


# --- real run (HIGH risk, approval-gated) -----------------------------------


def test_real_run_refused_without_approval(enabled: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = enabled
    with pytest.raises(ToolError) as exc:
        run_raw_action(doc_id, "object-to-path", dry_run=False, approval_token=None)
    assert "approval_token" in str(exc.value)
    # Nothing ran, nothing recorded.
    assert _LAST_ARGS == []
    assert not list(sandbox.operations_dir(root, doc_id).glob("op_*.json"))


def test_real_run_applied_records_and_reversible(enabled: tuple[str, Path, Path]) -> None:
    doc_id, root, src = enabled
    original = src.read_bytes()
    before_working = sandbox.working_copy(root, doc_id).read_bytes()

    result = run_raw_action(
        doc_id,
        "object-to-path",
        dry_run=False,
        approval_token=TOKEN,
    )

    assert result.dry_run is False
    assert result.changed is True
    assert result.plan.actions_argument == "object-to-path"
    assert result.operation_id is not None
    assert result.snapshot_id is not None
    assert result.preview_before is not None
    assert result.preview_after is not None

    # The argv that ran is an arg list with the single --actions element (no shell string).
    assert _LAST_ARGS[0].endswith("document.svg")
    assert "--actions=object-to-path" in _LAST_ARGS
    assert not any(" " in a and ";" in a for a in _LAST_ARGS if not a.startswith("--actions="))

    # Working copy now holds the engine output (one path).
    paths = _working_root(root, doc_id).findall(f".//{{{SVG_NS}}}path")
    assert len(paths) == 1

    # Operation record persisted.
    op_file = sandbox.operations_dir(root, doc_id) / f"{result.operation_id}.json"
    assert op_file.is_file()

    # The ORIGINAL source is byte-unchanged.
    assert src.read_bytes() == original

    # Reversible: restoring the pre-op snapshot returns the working copy to its prior bytes.
    restore_snapshot(doc_id, result.snapshot_id)
    assert sandbox.working_copy(root, doc_id).read_bytes() == before_working


def test_real_run_unknown_doc(enabled: tuple[str, Path, Path]) -> None:
    with pytest.raises(ToolError) as exc:
        run_raw_action("doc_unknown", "object-to-path", dry_run=False, approval_token=TOKEN)
    assert "document id not found" in str(exc.value)


def test_real_run_engine_failure_refused(
    enabled: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _root, _ = enabled

    def failing(args: list[str], settings: object | None = None) -> ProcessResult:
        return ProcessResult(
            args=list(args),
            returncode=1,
            stdout="",
            stderr="boom",
            duration_s=0.01,
            timed_out=False,
        )

    monkeypatch.setattr(chain_engine, "run_inkscape", failing)
    with pytest.raises(ToolError) as exc:
        run_raw_action(doc_id, "object-to-path", dry_run=False, approval_token=TOKEN)
    assert "action chain failed" in str(exc.value)


# --- registration -----------------------------------------------------------


def test_raw_action_tool_registered() -> None:
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "run_raw_action" in names
