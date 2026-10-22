"""`actions` (advanced): validate / run / raw with the Inkscape touch points faked."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.actions import chains as chain_engine
from inkscape_mcp.actions.capability_map import ActionCapabilityMap
from inkscape_mcp.config import ENV_RAW_ACTION_ENABLED, get_settings
from inkscape_mcp.workspace.subprocess_exec import ProcessResult
from tests.v2_support import call, open_doc, working_svg

PATHS = (
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
_MAP = ActionCapabilityMap(
    inkscape_version="1.4.3",
    inkscape_version_tuple=(1, 4, 3),
    actions=["select-by-id", "object-to-path", "path-union"],
    action_count=3,
    probed_at=datetime.now(UTC).isoformat(),
)
CHAIN = [{"action": "select-by-id", "args": ["p1"]}, {"action": "object-to-path"}]


@pytest.fixture(autouse=True)
def engine(monkeypatch, v2_renders):
    monkeypatch.setattr(chain_engine, "get_or_build_action_map", lambda **kw: _MAP)
    ran: list[list[str]] = []

    def fake_run_inkscape(args, settings=None):
        ran.append(list(args))
        out = next(a[len("--export-filename=") :] for a in args if a.startswith("--export-file"))
        Path(out).write_bytes(ENGINE_OUTPUT)
        return ProcessResult(
            args=list(args), returncode=0, stdout="", stderr="", duration_s=0.0, timed_out=False
        )

    monkeypatch.setattr(chain_engine, "run_inkscape", fake_run_inkscape)
    return ran


def _actions(app, **args):
    return call(app, "actions", args).data


def test_validate_returns_the_plan_without_running(v2_app, v2_ws, engine) -> None:
    plan = _actions(v2_app, action="validate", steps=CHAIN)
    assert plan["steps"][0]["action"] == "select-by-id"
    assert engine == []


def test_run_needs_approval_then_mutates_with_one_snapshot(v2_app, v2_ws, engine) -> None:
    doc = open_doc(v2_ws, "p.svg", PATHS)
    with pytest.raises(ToolError, match="HIGH risk"):
        _actions(v2_app, action="run", doc_id=doc, steps=CHAIN)
    res = _actions(v2_app, action="run", doc_id=doc, steps=CHAIN, approval_token="ok")
    assert res["changed"] is True and res["snapshot_id"] and res["plan"]["actions_argument"]
    assert 'id="p2"' not in working_svg(doc)
    assert len(engine) == 1


def test_validation_failure_is_chain_shaped_and_actionable(v2_app, v2_ws) -> None:
    doc = open_doc(v2_ws, "p.svg", PATHS)
    with pytest.raises(ToolError, match=r"action_absent.*actions\(action='list'\)"):
        _actions(
            v2_app,
            action="run",
            doc_id=doc,
            steps=[{"action": "path-difference"}],
            approval_token="ok",
        )
    with pytest.raises(ToolError, match="at least one"):
        _actions(v2_app, action="validate", steps=[])


def test_raw_defaults_to_a_dry_run(v2_app, v2_ws, engine) -> None:
    doc = open_doc(v2_ws, "p.svg", PATHS)
    res = _actions(v2_app, action="raw", doc_id=doc, steps=[{"action": "object-to-path"}])
    assert res["dry_run"] is True and res["changed"] is False
    assert engine == []
    assert working_svg(doc).count("<path") == 2


def test_raw_real_run_needs_approval_and_exactly_one_step(v2_app, v2_ws, engine) -> None:
    doc = open_doc(v2_ws, "p.svg", PATHS)
    step = [{"action": "object-to-path"}]
    with pytest.raises(ToolError, match="HIGH risk"):
        _actions(v2_app, action="raw", doc_id=doc, steps=step, dry_run=False)
    with pytest.raises(ToolError, match="exactly one step"):
        _actions(v2_app, action="raw", doc_id=doc, steps=CHAIN)
    res = _actions(v2_app, action="raw", doc_id=doc, steps=step, dry_run=False, approval_token="ok")
    assert res["changed"] is True


def test_raw_is_refused_when_advanced_mode_is_off(v2_app, v2_ws, monkeypatch) -> None:
    monkeypatch.setenv(ENV_RAW_ACTION_ENABLED, "0")
    get_settings.cache_clear()
    doc = open_doc(v2_ws, "p.svg", PATHS)
    with pytest.raises(ToolError, match="raw_action_disabled"):
        _actions(v2_app, action="raw", doc_id=doc, steps=[{"action": "object-to-path"}])


def test_missing_arguments(v2_app, v2_ws) -> None:
    with pytest.raises(ToolError, match="requires doc_id"):
        _actions(v2_app, action="run", steps=CHAIN, approval_token="ok")
    with pytest.raises(ToolError, match="document id not found"):
        _actions(v2_app, action="run", doc_id="nope", steps=CHAIN, approval_token="ok")


def test_list_and_extensions_are_read_only(v2_app, monkeypatch) -> None:
    monkeypatch.setattr("inkscape_mcp.tools.actions.persist_action_map", lambda *_a, **_k: None)
    listing = _actions(v2_app, action="list", all=False)
    assert "actions" not in listing
    assert isinstance(_actions(v2_app, action="extensions"), dict)
