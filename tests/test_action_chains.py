"""Controlled Action chain tests (ADR-002 / ADR-003 / ADR-004 / sec.12).

Hermetic: the Inkscape touch points are monkeypatched — `render_preview` in the pipeline (before/
after frames), `run_inkscape` in the chain engine (the Action run), and `get_or_build_action_map` so
validation consults a synthetic capability map instead of probing. Covers: allowlist refusal,
absent-Action refusal (graceful degradation via the versioned map), malformed action/arg refusal,
dry-run produces no mutation, HIGH execution refused without an approval token, applied run records
a snapshot + Operation Record + previews (reversible), and arg-list assembly (no shell string).
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
from inkscape_mcp.actions.chains import (
    ActionChainError,
    ActionStep,
    validate_chain,
)
from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.edit import pipeline
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render.cli import RenderResult
from inkscape_mcp.server import mcp
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.tools import actions as actions_tools
from inkscape_mcp.tools.actions import run_action_chain, validate_action_chain
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
#: deliberately ABSENT to test graceful degradation.
_MAP = ActionCapabilityMap(
    inkscape_version="1.4.3",
    inkscape_version_tuple=(1, 4, 3),
    actions=["select-by-id", "object-to-path", "path-union", "path-difference"],
    action_count=4,
    probed_at=datetime.now(UTC).isoformat(),
)

_LAST_ARGS: list[str] = []


@pytest.fixture
def doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
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
        # one-location contract: artifact_path is workspace-ROOT-relative (matches engine).
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


# --- validation / allowlist / capability map --------------------------------


def test_validate_chain_happy(doc: tuple[str, Path, Path]) -> None:
    plan = validate_chain(
        [
            ActionStep(action="select-by-id", args=["p1", "p2"]),
            ActionStep(action="path-union"),
        ]
    )
    assert plan.valid is True
    assert plan.actions_argument == "select-by-id:p1,p2;path-union"
    assert plan.inkscape_version == "1.4.3"
    # argv preview is an arg list, not a shell string.
    assert any(a.startswith("--actions=") for a in plan.argv_preview)


def test_validate_chain_normalizes_whitespace(doc: tuple[str, Path, Path]) -> None:
    # Surrounding whitespace is stripped so it can never survive into the --actions argv element.
    plan = validate_chain(
        [ActionStep(action="  select-by-id ", args=[" p1 ", "p2"]), ActionStep(action="path-union")]
    )
    assert plan.actions_argument == "select-by-id:p1,p2;path-union"
    assert plan.steps[0].action == "select-by-id"
    assert plan.steps[0].args == ["p1", "p2"]


def test_non_allowlisted_action_refused(doc: tuple[str, Path, Path]) -> None:
    # `effect.voronoi` exists on no allowlist (and would not be in the map anyway).
    with pytest.raises(ActionChainError) as exc:
        validate_chain([ActionStep(action="effect.voronoi")])
    assert exc.value.code == "not_allowlisted"


def test_allowlisted_but_absent_action_refused(doc: tuple[str, Path, Path]) -> None:
    # `path-intersection` IS in the default allowlist but is NOT in the synthetic map ⇒ degrade.
    with pytest.raises(ActionChainError) as exc:
        validate_chain([ActionStep(action="path-intersection")])
    assert exc.value.code == "action_absent"


def test_malformed_action_refused(doc: tuple[str, Path, Path]) -> None:
    with pytest.raises(ActionChainError) as exc:
        validate_chain([ActionStep(action="bad action;rm -rf")])
    assert exc.value.code in {"malformed_action", "not_allowlisted"}


def test_malformed_arg_refused(doc: tuple[str, Path, Path]) -> None:
    # A ';' in an arg would break the step grammar — rejected by the arg charset.
    with pytest.raises(ActionChainError) as exc:
        validate_chain([ActionStep(action="select-by-id", args=["p1;evil"])])
    assert exc.value.code == "malformed_arg"


def test_comma_joined_select_by_id_arg_refused_with_hint(doc: tuple[str, Path, Path]) -> None:
    # S15: a comma-joined multi-id token is the documented foot-gun. It is rejected as
    # `malformed_arg`, but the message must carry an actionable HINT steering to separate tokens.
    with pytest.raises(ActionChainError) as exc:
        validate_chain([ActionStep(action="select-by-id", args=["p_a,p_b"])])
    assert exc.value.code == "malformed_arg"
    msg = exc.value.message
    assert "HINT" in msg
    assert "separate arg token" in msg.lower()
    # The hint shows the corrected shape with each id as its own token.
    assert "'p_a'" in msg
    assert "'p_b'" in msg


def test_separate_token_multi_id_selection_accepted(doc: tuple[str, Path, Path]) -> None:
    # The DOCUMENTED multi-id form — separate tokens — validates and the engine joins them with
    # commas into the --actions grammar itself.
    plan = validate_chain([ActionStep(action="select-by-id", args=["p_a", "p_b"])])
    assert plan.actions_argument == "select-by-id:p_a,p_b"


def test_plain_malformed_arg_has_no_comma_hint(doc: tuple[str, Path, Path]) -> None:
    # A non-comma malformed arg keeps the bare message (the hint is comma-specific only).
    with pytest.raises(ActionChainError) as exc:
        validate_chain([ActionStep(action="select-by-id", args=["p1;evil"])])
    assert exc.value.code == "malformed_arg"
    assert "HINT" not in exc.value.message


def test_empty_chain_refused(doc: tuple[str, Path, Path]) -> None:
    with pytest.raises(ActionChainError) as exc:
        validate_chain([])
    assert exc.value.code == "empty_chain"


def test_chain_too_long_refused(doc: tuple[str, Path, Path]) -> None:
    steps = [ActionStep(action="object-to-path")] * (chain_engine.MAX_CHAIN_STEPS + 1)
    with pytest.raises(ActionChainError) as exc:
        validate_chain(steps)
    assert exc.value.code == "chain_too_long"


def test_allowlist_is_server_side_not_client_supplied(doc: tuple[str, Path, Path]) -> None:
    # The allowlist comes from Settings; a tool/chain caller cannot add to it. An action absent
    # from BOTH the default allowlist and the map is refused regardless of how it is requested.
    with pytest.raises(ActionChainError):
        validate_chain([ActionStep(action="file-save")])


# --- dry-run tool (low risk, no mutation) -----------------------------------


def test_validate_action_chain_tool_no_mutation(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    plan = validate_action_chain(
        [
            ActionStep(action="select-by-id", args=["p1"]),
            ActionStep(action="object-to-path"),
        ]
    )
    assert plan.actions_argument == "select-by-id:p1;object-to-path"
    # No engine invocation, no write, no operation record.
    assert _LAST_ARGS == []
    assert sandbox.working_copy(root, doc_id).read_bytes() == before
    assert not list(sandbox.operations_dir(root, doc_id).glob("op_*.json"))


def test_validate_action_chain_tool_refuses_bad_chain(doc: tuple[str, Path, Path]) -> None:
    with pytest.raises(ToolError) as exc:
        validate_action_chain([ActionStep(action="effect.voronoi")])
    assert "not_allowlisted" in str(exc.value)


def test_validate_action_chain_tool_surfaces_comma_id_hint(doc: tuple[str, Path, Path]) -> None:
    # S15: the comma-joined-id hint reaches the agent through the tool's ToolError.
    with pytest.raises(ToolError) as exc:
        validate_action_chain([ActionStep(action="select-by-id", args=["p_a,p_b"])])
    text = str(exc.value)
    assert "malformed_arg" in text
    assert "HINT" in text
    assert "separate arg token" in text.lower()


# --- run_action_chain (HIGH risk, approval-gated) ---------------------------


def test_run_chain_refused_without_approval(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    with pytest.raises(ToolError) as exc:
        run_action_chain(doc_id, [ActionStep(action="object-to-path")], approval_token=None)
    assert "approval_token" in str(exc.value)
    # Nothing ran, nothing recorded.
    assert _LAST_ARGS == []
    assert not list(sandbox.operations_dir(root, doc_id).glob("op_*.json"))


def test_run_chain_applied_records_and_reversible(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, src = doc
    original = src.read_bytes()
    before_working = sandbox.working_copy(root, doc_id).read_bytes()

    result = run_action_chain(
        doc_id,
        [ActionStep(action="select-by-id", args=["p1", "p2"]), ActionStep(action="path-union")],
        approval_token=TOKEN,
    )

    assert result.changed is True
    assert result.plan.actions_argument == "select-by-id:p1,p2;path-union"
    assert result.operation_id is not None
    assert result.snapshot_id is not None
    assert result.preview_before is not None
    assert result.preview_after is not None

    # The argv that ran is an arg list with the single --actions element (no shell string).
    assert _LAST_ARGS[0].endswith("document.svg")
    assert "--actions=select-by-id:p1,p2;path-union" in _LAST_ARGS
    assert not any(" " in a and ";" in a for a in _LAST_ARGS if not a.startswith("--actions="))

    # Working copy now holds the engine output (one path).
    paths = _working_root(root, doc_id).findall(f".//{{{SVG_NS}}}path")
    assert len(paths) == 1

    # Operation record persisted, HIGH risk, approved.
    op_file = sandbox.operations_dir(root, doc_id) / f"{result.operation_id}.json"
    assert op_file.is_file()

    # The ORIGINAL source is byte-unchanged.
    assert src.read_bytes() == original

    # Reversible: restoring the pre-op snapshot returns the working copy to its prior bytes.
    restore_snapshot(doc_id, result.snapshot_id)
    assert sandbox.working_copy(root, doc_id).read_bytes() == before_working


def test_run_chain_unknown_doc(doc: tuple[str, Path, Path]) -> None:
    with pytest.raises(ToolError) as exc:
        run_action_chain("doc_unknown", [ActionStep(action="object-to-path")], approval_token=TOKEN)
    assert "document id not found" in str(exc.value)


def test_run_chain_engine_failure_refused(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _root, _ = doc

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
        run_action_chain(doc_id, [ActionStep(action="object-to-path")], approval_token=TOKEN)
    assert "action chain failed" in str(exc.value)


# --- registration -----------------------------------------------------------


def test_chain_tools_registered() -> None:
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "validate_action_chain" in names
    assert "run_action_chain" in names


def test_tools_module_imports_engine() -> None:
    # Sanity: the tool module routes through the shared engine (no duplicate validation logic).
    assert actions_tools.chain_engine is chain_engine


# ---: capability-absent action errors name the discovery / alt tools ---


def test_chain_error_message_action_absent_names_list_actions() -> None:
    #: an action absent from THIS runtime's capability map names list_actions; any other
    # chain error keeps its own `code: message`.
    from inkscape_mcp.actions.chains import ActionChainError
    from inkscape_mcp.tools.actions import _chain_error_message

    absent = _chain_error_message(ActionChainError("action_absent", "action 'x' is not available"))
    assert "list_actions" in absent
    assert absent.startswith("action_absent:")

    other = _chain_error_message(ActionChainError("not_allowlisted", "nope"))
    assert other == "not_allowlisted: nope"
    assert "list_actions" not in other


def test_engine_error_message_unavailable_names_list_capabilities() -> None:
    #: an absent engine (no binary) names list_capabilities; other engine errors unchanged.
    from inkscape_mcp.actions.chains import ActionEngineError
    from inkscape_mcp.tools.actions import _engine_error_message

    absent = _engine_error_message(ActionEngineError("inkscape engine unavailable"))
    assert "list_capabilities" in absent

    other = _engine_error_message(ActionEngineError("action chain timed out"))
    assert other == "action chain timed out"
