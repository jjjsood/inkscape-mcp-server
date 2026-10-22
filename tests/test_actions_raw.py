"""Controlled Action chains and the raw Action, at engine level and through `actions`.

Covers the full validation matrix of the chain engine (allowlist, capability map, charsets, length
caps), the argv/record/rollback guarantees of a run, the gate order of `actions(action='raw')`, and
the read-only `list` / `extensions` discovery actions.

Hermetic: `run_inkscape` of the chain engine is faked (it writes a canned SVG to the temp
``--export-filename``) and `build_action_map` returns a synthetic capability map, so the
real validation, argv assembly, output re-parse and DOM-replace bridge run end to end.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastmcp.exceptions import ToolError
from lxml import etree

from inkscape_mcp.actions import chains as chain_engine
from inkscape_mcp.actions.capability_map import ActionCapabilityMap
from inkscape_mcp.actions.chains import (
    MAX_CHAIN_STEPS,
    MAX_STEP_ARGS,
    ActionChainError,
    ActionStep,
    run_chain_op,
    validate_chain,
)
from inkscape_mcp.config import ENV_RAW_ACTION_ENABLED, get_settings
from inkscape_mcp.operations import OperationStatus, get_operation
from inkscape_mcp.registry import get_registry
from inkscape_mcp.snapshots import list_snapshots, restore_snapshot
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.subprocess_exec import ProcessError, ProcessResult
from tests.tool_support import call, open_doc

SVG_NS = "http://www.w3.org/2000/svg"
HEAD = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
PATHS = (
    HEAD + b'<path id="p1" d="M10,10 L50,10 L50,50 L10,50 Z" fill="red"/>'
    b'<path id="p2" d="M30,30 L70,30 L70,70 L30,70 Z" fill="blue"/></svg>'
)
ENGINE_OUTPUT = HEAD + b'<path id="p1" d="M10,10 L50,10 L50,50 L10,50 Z"/></svg>'
TOKEN = "approved-by-operator"

#: `path-intersection` is on the default allowlist but deliberately ABSENT here (graceful degrade)
_MAP = ActionCapabilityMap(
    inkscape_version="1.4.3",
    actions=["select-by-id", "object-to-path", "path-union", "path-difference"],
)
UNION_CHAIN = [{"action": "select-by-id", "args": ["p1", "p2"]}, {"action": "path-union"}]
TO_PATH = [{"action": "object-to-path"}]


class FakeEngine:
    """Stand-in for `run_inkscape` in the chain engine; records every argv."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []
        self.returncode = 0
        self.timed_out = False
        self.error: Exception | None = None
        self.output = ENGINE_OUTPUT

    def __call__(self, args: list[str], settings: object | None = None) -> ProcessResult:
        self.argvs.append(list(args))
        if self.error is not None:
            raise self.error
        if self.returncode == 0 and not self.timed_out:
            out = next(a for a in args if a.startswith("--export-filename="))
            Path(out.split("=", 1)[1]).write_bytes(self.output)
        return ProcessResult(
            args=list(args),
            returncode=self.returncode,
            stdout="",
            stderr="",
            duration_s=0.0,
            timed_out=self.timed_out,
        )


@pytest.fixture
def eng(monkeypatch: pytest.MonkeyPatch, tool_ws: Path, tool_renders: list[str]) -> FakeEngine:
    fake = FakeEngine()
    monkeypatch.setattr(chain_engine, "build_action_map", lambda *a, **kw: _MAP)
    monkeypatch.setattr(chain_engine, "run_inkscape", fake)
    return fake


@pytest.fixture
def doc(eng: FakeEngine, tool_ws: Path) -> str:
    return open_doc(tool_ws, "shapes.svg", PATHS)


def _actions(app: Any, **args: Any) -> dict[str, Any]:
    return call(app, "actions", args).data


def _step(action: str, *args: str) -> ActionStep:
    return ActionStep(action=action, args=list(args))


def _bytes(ws: Path, doc_id: str) -> bytes:
    return sandbox.working_copy(ws, doc_id).read_bytes()


def _records(ws: Path, doc_id: str) -> list[Path]:
    d = sandbox.operations_dir(ws, doc_id)
    return sorted(d.glob("op_*.json")) if d.is_dir() else []


# --- chain validation (engine) ------------------------------------------------


def test_validate_chain_builds_the_plan_without_the_engine(eng: FakeEngine) -> None:
    plan = validate_chain([_step("select-by-id", "p1", "p2"), _step("path-union")])
    assert plan.valid and plan.inkscape_version == "1.4.3"
    assert plan.actions_argument == "select-by-id:p1,p2;path-union"
    assert plan.argv_preview[1] == "--actions=select-by-id:p1,p2;path-union"  # an arg list
    assert eng.argvs == []


def test_whitespace_is_stripped_before_it_can_reach_argv(eng: FakeEngine) -> None:
    plan = validate_chain([_step("  select-by-id ", " p1 ", "p2"), _step("path-union")])
    assert plan.actions_argument == "select-by-id:p1,p2;path-union"
    assert plan.steps[0] == _step("select-by-id", "p1", "p2")


@pytest.mark.parametrize(
    ("steps", "code"),
    [
        ([_step("effect.voronoi")], "not_allowlisted"),
        ([_step("file-save")], "not_allowlisted"),  # the allowlist is server-side only
        ([_step("path-intersection")], "action_absent"),  # allowlisted but not on this runtime
        ([_step("select-by-id", "p1;evil")], "malformed_arg"),
        ([_step("select-by-id", "")], "malformed_arg"),
        ([_step("select-by-id", *["p"] * (MAX_STEP_ARGS + 1))], "too_many_args"),
        ([], "empty_chain"),
        ([_step("object-to-path")] * (MAX_CHAIN_STEPS + 1), "chain_too_long"),
    ],
)
def test_invalid_chains_carry_a_machine_readable_code(
    eng: FakeEngine, steps: list[ActionStep], code: str
) -> None:
    with pytest.raises(ActionChainError) as exc:
        validate_chain(steps)
    assert exc.value.code == code


@pytest.mark.parametrize("action", ["bad action;rm -rf", "path-union;file-save", "path-union x"])
def test_malformed_action_ids_are_refused(eng: FakeEngine, action: str) -> None:
    # The charset check runs BEFORE the allowlist: an injection-shaped id never reaches it.
    with pytest.raises(ActionChainError) as exc:
        validate_chain([_step(action)])
    assert exc.value.code == "malformed_action"


def test_comma_joined_ids_get_an_actionable_hint_but_other_bad_args_do_not(eng: FakeEngine) -> None:
    with pytest.raises(ActionChainError) as exc:
        validate_chain([_step("select-by-id", "p_a,p_b")])
    msg = exc.value.message
    assert exc.value.code == "malformed_arg" and "HINT" in msg
    assert "separate arg token" in msg.lower() and "'p_a'" in msg and "'p_b'" in msg
    with pytest.raises(ActionChainError) as plain:
        validate_chain([_step("select-by-id", "p1;evil")])
    assert "HINT" not in plain.value.message


def test_run_chain_op_revalidates_before_touching_the_engine(
    eng: FakeEngine, doc: str, tool_ws: Path
) -> None:
    tree = etree.ElementTree(etree.fromstring(PATHS))
    with pytest.raises(ActionChainError):
        run_chain_op(tree, Path(get_registry().get(doc).working_path), [_step("effect.voronoi")])
    assert eng.argvs == []
    summary = run_chain_op(
        tree, Path(get_registry().get(doc).working_path), [_step("object-to-path")]
    )
    assert "1 step(s)" in summary and len(tree.getroot().findall(f"{{{SVG_NS}}}path")) == 1


# --- `validate`: the dry plan ----------------------------------------------------


def test_validate_returns_argv_preview_and_version_and_writes_nothing(
    tool_app, tool_ws: Path, doc: str, eng: FakeEngine
) -> None:
    before = _bytes(tool_ws, doc)
    plan = _actions(tool_app, action="validate", steps=UNION_CHAIN)
    assert plan["actions_argument"] == "select-by-id:p1,p2;path-union"
    assert any(a.startswith("--actions=") for a in plan["argv_preview"])
    assert plan["inkscape_version"] == "1.4.3"
    assert eng.argvs == [] and _bytes(tool_ws, doc) == before and _records(tool_ws, doc) == []


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([{"action": "effect.voronoi"}], "not_allowlisted"),
        ([{"action": "path-intersection"}], r"action_absent.*actions\(action='list'\)"),
        ([{"action": "select-by-id", "args": ["p_a,p_b"]}], "malformed_arg.*HINT.*SEPARATE arg"),
        ([], "at least one"),
    ],
)
def test_validate_refuses_a_bad_chain_with_code_and_hint(
    tool_app, eng: FakeEngine, steps, message: str
) -> None:
    with pytest.raises(ToolError, match=message):
        _actions(tool_app, action="validate", steps=steps)


# --- `run`: HIGH risk, one recorded, reversible edit -------------------------------


@pytest.mark.parametrize("token", [None, "", "  "])
def test_run_without_a_token_runs_nothing(
    tool_app, tool_ws: Path, doc: str, eng: FakeEngine, token: str | None
) -> None:
    with pytest.raises(ToolError, match="approval_token"):
        _actions(tool_app, action="run", doc_id=doc, steps=TO_PATH, approval_token=token)
    assert eng.argvs == [] and _records(tool_ws, doc) == []


def test_run_applies_records_and_is_reversible(
    tool_app, tool_ws: Path, doc: str, eng: FakeEngine, before_after_previews: None
) -> None:
    before = _bytes(tool_ws, doc)
    res = _actions(tool_app, action="run", doc_id=doc, steps=UNION_CHAIN, approval_token=TOKEN)
    assert res["changed"] is True
    assert res["plan"]["actions_argument"] == "select-by-id:p1,p2;path-union"

    # exactly one argv: an arg list whose single --actions element is the validated chain
    (argv,) = eng.argvs
    assert "--actions=select-by-id:p1,p2;path-union" in argv
    assert not any(" " in a and ";" in a for a in argv if not a.startswith("--actions="))

    working = etree.fromstring(_bytes(tool_ws, doc))
    assert len(working.findall(f".//{{{SVG_NS}}}path")) == 1
    record = get_operation(doc, res["operation_id"])
    assert record.tool == "actions.run" and record.status is OperationStatus.APPLIED
    assert record.risk_class.value == "high" and record.policy_decision["approved"] is True
    assert record.snapshot_id == res["snapshot_id"]
    assert set(record.previews) == {"before", "after"}
    assert [s.snapshot_id for s in list_snapshots(doc)] == [res["snapshot_id"]]
    assert (tool_ws / "shapes.svg").read_bytes() == PATHS  # the original is untouched

    restore_snapshot(doc, res["snapshot_id"])
    assert _bytes(tool_ws, doc) == before


def test_a_refused_chain_is_validated_before_the_gate_leaves_no_applied_record(
    tool_app, tool_ws: Path, doc: str, eng: FakeEngine
) -> None:
    with pytest.raises(ToolError, match="not_allowlisted"):
        _actions(
            tool_app,
            action="run",
            doc_id=doc,
            steps=[{"action": "effect.voronoi"}],
            approval_token=TOKEN,
        )
    assert eng.argvs == []
    assert list_snapshots(doc) == []


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ({"returncode": 1}, "action chain failed"),
        ({"timed_out": True}, "action chain timed out"),
        ({"output": b"  "}, "action chain produced empty output"),
        (
            {"error": ProcessError("no binary")},
            r"inkscape engine unavailable.*help\(topic='diagnose'",
        ),
    ],
)
def test_engine_failures_write_nothing_and_leave_no_applied_record(
    tool_app, tool_ws: Path, doc: str, eng: FakeEngine, setup: dict[str, Any], message: str
) -> None:
    before = _bytes(tool_ws, doc)
    for key, value in setup.items():
        setattr(eng, key, value)
    with pytest.raises(ToolError, match=message):
        _actions(tool_app, action="run", doc_id=doc, steps=TO_PATH, approval_token=TOKEN)
    assert _bytes(tool_ws, doc) == before
    assert list_snapshots(doc) == []
    for path in _records(tool_ws, doc):
        assert OperationStatus.APPLIED.value not in path.read_text()


# --- `raw`: one step, gated in a fixed order -----------------------------------------


@pytest.fixture
def raw_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_RAW_ACTION_ENABLED, "0")
    get_settings.cache_clear()


def test_the_disabled_gate_is_checked_before_any_validation(
    tool_app, tool_ws: Path, doc: str, eng: FakeEngine, raw_off: None
) -> None:
    # even a non-allowlisted action reports the gate, not a validation error: nothing is attempted
    with pytest.raises(ToolError, match="raw_action_disabled"):
        _actions(tool_app, action="raw", doc_id=doc, steps=[{"action": "effect.voronoi"}])
    assert eng.argvs == [] and _records(tool_ws, doc) == []


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ({"action": "effect.voronoi"}, "not_allowlisted"),
        ({"action": "path-intersection"}, "action_absent"),
        ({"action": "select-by-id", "args": ["p1;evil"]}, "malformed_arg"),
        ({"action": "select-by-id", "args": ["p_a,p_b"]}, "malformed_arg.*HINT"),
    ],
)
def test_raw_refuses_invalid_steps_even_as_a_dry_run(
    tool_app, doc: str, eng: FakeEngine, step: dict[str, Any], message: str
) -> None:
    with pytest.raises(ToolError, match=message):
        _actions(tool_app, action="raw", doc_id=doc, steps=[step])
    assert eng.argvs == []


def test_raw_dry_run_shows_the_resolved_argv_and_changes_nothing(
    tool_app, tool_ws: Path, doc: str, eng: FakeEngine
) -> None:
    before = _bytes(tool_ws, doc)
    res = _actions(
        tool_app, action="raw", doc_id=doc, steps=[{"action": "select-by-id", "args": ["p1", "p2"]}]
    )
    assert res["dry_run"] is True and res["changed"] is False
    assert res["plan"]["actions_argument"] == "select-by-id:p1,p2"
    assert any(a.startswith("--actions=") for a in res["plan"]["argv_preview"])
    assert "operation_id" not in res and "snapshot_id" not in res
    assert eng.argvs == [] and _bytes(tool_ws, doc) == before and _records(tool_ws, doc) == []


def test_raw_real_run_without_a_token_runs_nothing(
    tool_app, tool_ws: Path, doc: str, eng: FakeEngine
) -> None:
    with pytest.raises(ToolError, match="approval_token"):
        _actions(tool_app, action="raw", doc_id=doc, steps=TO_PATH, dry_run=False)
    assert eng.argvs == [] and _records(tool_ws, doc) == []


def test_raw_real_run_applies_one_recorded_reversible_step(
    tool_app, tool_ws: Path, doc: str, eng: FakeEngine
) -> None:
    before = _bytes(tool_ws, doc)
    res = _actions(
        tool_app, action="raw", doc_id=doc, steps=TO_PATH, dry_run=False, approval_token=TOKEN
    )
    assert res["changed"] is True and res["plan"]["actions_argument"] == "object-to-path"
    assert "--actions=object-to-path" in eng.argvs[0]
    record = get_operation(doc, res["operation_id"])
    assert record.tool == "actions.raw" and record.status is OperationStatus.APPLIED
    assert len(etree.fromstring(_bytes(tool_ws, doc)).findall(f".//{{{SVG_NS}}}path")) == 1
    restore_snapshot(doc, res["snapshot_id"])
    assert _bytes(tool_ws, doc) == before


def test_raw_takes_exactly_one_step(tool_app, doc: str, eng: FakeEngine) -> None:
    with pytest.raises(ToolError, match="exactly one step"):
        _actions(tool_app, action="raw", doc_id=doc, steps=UNION_CHAIN)
    assert eng.argvs == []


@pytest.mark.parametrize("action", ["run", "raw"])
def test_run_and_raw_need_a_known_document_and_a_doc_id(
    tool_app, eng: FakeEngine, action: str
) -> None:
    with pytest.raises(ToolError, match="requires doc_id"):
        _actions(tool_app, action=action, steps=TO_PATH, approval_token=TOKEN)
    with pytest.raises(ToolError, match="document id not found"):
        _actions(
            tool_app,
            action=action,
            doc_id="doc_unknown",
            steps=TO_PATH,
            dry_run=False,
            approval_token=TOKEN,
        )
    assert eng.argvs == []


# --- `list` / `extensions`: read-only discovery ---------------------------------------


def test_list_and_extensions_report_the_server_side_allowlists(tool_app) -> None:
    listing = _actions(tool_app, action="list", all=False)
    assert "actions" not in listing  # all=False drops the full host Action list
    assert "path-union" in listing["allowlisted"]
    assert set(listing.get("available", [])) <= set(listing["allowlisted"])
    ext = _actions(tool_app, action="extensions")
    # Extension execution is opt-in: the default allowlist is empty and says so (sec.12).
    assert not ext.get("allowlisted")
    assert any("OFF by default" in note for note in ext["notes"])
