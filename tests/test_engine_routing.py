"""render/export + path/chain routing through the warm engine, with fallback.

Two layers:
- Fake-shell / monkeypatched unit tests (run everywhere): prove `engine_mode=shell` routes through
  the engine and that ANY engine fault transparently falls back to the per-call CLI.
- `@pytest.mark.inkscape` parity tests (run only with a real binary): prove the warm worker produces
  BYTE-EQUIVALENT artifacts vs per-call, the mutating path matches, and the warm path is faster.
"""

from __future__ import annotations

import hashlib
import shutil
import struct
import time
from pathlib import Path

import pytest

from inkscape_mcp.config import (
    ENGINE_MODE_PER_CALL,
    ENGINE_MODE_SHELL,
    ENV_WORKSPACE_ROOTS,
    Settings,
    get_settings,
)
from inkscape_mcp.engine.manager import reset_engine_manager
from inkscape_mcp.engine.process import EngineUnavailable
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render import cli as render_cli
from inkscape_mcp.workspace.subprocess_exec import ProcessResult

SVG = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" width="100" height="60" viewBox="0 0 100 60">
  <rect id="a" x="10" y="10" width="40" height="30" fill="#3366cc"/>
  <rect id="b" x="30" y="20" width="40" height="30" fill="#cc3366"/>
</svg>
"""

inkscape_available = shutil.which("inkscape") is not None


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    reset_engine_manager()
    return ws


@pytest.fixture
def doc_id(root: Path) -> str:
    src = root / "art.svg"
    src.write_bytes(SVG)
    return get_registry().open_document(str(src)).doc_id


def _valid_png(path: Path, w: int = 100, h: int = 60) -> None:
    from PIL import Image

    Image.new("RGB", (w, h), (220, 220, 220)).save(path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- routing (no Inkscape) -------------------------------------------------


def test_shell_mode_routes_render_through_engine(
    doc_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, int] = {"engine": 0}

    def fake_engine(working_path: Path, out: Path, *, fmt: str, width_px, settings) -> None:
        calls["engine"] += 1
        _valid_png(out, width_px or 100, 60)

    monkeypatch.setattr(render_cli, "engine_export_document", fake_engine)
    # Prove the per-call CLI is NOT touched when the engine path succeeds.
    monkeypatch.setattr(
        render_cli, "run_inkscape", lambda *a, **k: pytest.fail("per-call CLI should not run")
    )
    result = render_cli.render_preview(doc_id, settings=Settings(engine_mode=ENGINE_MODE_SHELL))
    assert calls["engine"] == 1
    assert result.format == "png"


def test_engine_fault_falls_back_to_per_call(doc_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k) -> None:
        raise EngineUnavailable("induced engine fault")

    used = {"per_call": 0}

    def fake_run_inkscape(args, settings=None) -> ProcessResult:
        used["per_call"] += 1
        # The per-call argv ends with --export-filename=<out>; honor it so finalize succeeds.
        out = next(a.split("=", 1)[1] for a in args if a.startswith("--export-filename="))
        _valid_png(Path(out))
        return ProcessResult(
            args=list(args), returncode=0, stdout="", stderr="", duration_s=0.01, timed_out=False
        )

    monkeypatch.setattr(render_cli, "engine_export_document", boom)
    monkeypatch.setattr(render_cli, "run_inkscape", fake_run_inkscape)
    result = render_cli.render_preview(doc_id, settings=Settings(engine_mode=ENGINE_MODE_SHELL))
    assert used["per_call"] == 1  # fault fell back to the per-call CLI
    assert result.format == "png"


def test_per_call_mode_never_touches_engine(doc_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        render_cli,
        "engine_export_document",
        lambda *a, **k: pytest.fail("engine must not run in per_call mode"),
    )

    def fake_run_inkscape(args, settings=None) -> ProcessResult:
        out = next(a.split("=", 1)[1] for a in args if a.startswith("--export-filename="))
        _valid_png(Path(out))
        return ProcessResult(
            args=list(args), returncode=0, stdout="", stderr="", duration_s=0.01, timed_out=False
        )

    monkeypatch.setattr(render_cli, "run_inkscape", fake_run_inkscape)
    render_cli.render_preview(doc_id, settings=Settings(engine_mode=ENGINE_MODE_PER_CALL))


def test_object_export_pdf_stay_per_call(doc_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        render_cli,
        "engine_export_document",
        lambda *a, **k: pytest.fail("object/PDF must not use the warm engine"),
    )

    def fake_run_inkscape(args, settings=None) -> ProcessResult:
        out = next(a.split("=", 1)[1] for a in args if a.startswith("--export-filename="))
        outp = Path(out)
        if outp.suffix == ".png":
            _valid_png(outp)
        else:
            outp.write_bytes(b"%PDF-1.5\n")
        return ProcessResult(
            args=list(args), returncode=0, stdout="", stderr="", duration_s=0.01, timed_out=False
        )

    monkeypatch.setattr(render_cli, "run_inkscape", fake_run_inkscape)
    s = Settings(engine_mode=ENGINE_MODE_SHELL)
    render_cli.export_object(doc_id, "a", "png", settings=s)  # object -> per-call
    render_cli.export_document(doc_id, "pdf", settings=s)  # PDF -> per-call


# --- real-binary parity ----------------------------------------------------


@pytest.mark.inkscape
def test_byte_equivalent_whole_doc_png_svg(doc_id: str) -> None:
    per_call = Settings(engine_mode=ENGINE_MODE_PER_CALL)
    shell = Settings(engine_mode=ENGINE_MODE_SHELL)
    try:
        for fmt in ("png", "svg"):
            pc = render_cli.export_document(doc_id, fmt, settings=per_call)
            reset_engine_manager()
            sh = render_cli.export_document(doc_id, fmt, settings=shell)
            #: artifact_path is workspace-ROOT-relative now.
            pc_path = Path(get_registry().get(doc_id).root) / pc.artifact_path
            sh_path = Path(get_registry().get(doc_id).root) / sh.artifact_path
            assert _sha(pc_path) == _sha(sh_path), f"{fmt} differs between modes"
    finally:
        reset_engine_manager()


@pytest.mark.inkscape
def test_mutating_path_op_matches_per_call(doc_id: str) -> None:
    from inkscape_mcp.edit import paths as engine

    working = Path(get_registry().get(doc_id).working_path)
    pc = engine.run_path_op(working, engine.UNION, ["a", "b"], settings=Settings())
    reset_engine_manager()
    sh = engine.run_path_op(
        working, engine.UNION, ["a", "b"], settings=Settings(engine_mode=ENGINE_MODE_SHELL)
    )
    reset_engine_manager()
    # Both run `select-by-id:a,b;path-union` then export plain SVG of the same input -> identical.
    assert sh == pc


@pytest.mark.inkscape
def test_warm_engine_faster_than_per_call_for_batch(doc_id: str) -> None:
    n = 6
    per_call = Settings(engine_mode=ENGINE_MODE_PER_CALL)
    shell = Settings(engine_mode=ENGINE_MODE_SHELL)
    try:
        t0 = time.monotonic()
        for _ in range(n):
            render_cli.export_document(doc_id, "png", width_px=128, settings=per_call)
        per_call_s = time.monotonic() - t0

        reset_engine_manager()
        # Warm the worker once (excludes the one-time spawn from the measured batch).
        render_cli.export_document(doc_id, "png", width_px=128, settings=shell)
        t1 = time.monotonic()
        for _ in range(n):
            render_cli.export_document(doc_id, "png", width_px=128, settings=shell)
        shell_s = time.monotonic() - t1

        assert shell_s < per_call_s, (
            f"warm {shell_s:.3f}s not faster than per-call {per_call_s:.3f}s"
        )
    finally:
        reset_engine_manager()


def _png_dims(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    return struct.unpack(">II", head[16:24])
