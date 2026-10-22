"""Change detection: state tokens, delta classification, bounded wait, `live(action=wait)`.

Interval/timeout/clock are injected at engine level so every test stays fast; the tool tests use
tiny budgets. Change detection is read-only: it never records a Live Operation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.config import ENV_LIVE_ENABLED, ENV_WORKSPACE_ROOTS, Settings, get_settings
from inkscape_mcp.live import session as session_mod
from inkscape_mcp.live.events import (
    MAX_WAIT_TIMEOUT_S,
    classify_change,
    detect_change,
    token_from_result,
    wait_for_change,
)
from inkscape_mcp.live.protocol import PROTOCOL_VERSION, LiveCommand
from inkscape_mcp.live.records import list_live_operations
from inkscape_mcp.live.session import LiveSessionManager, get_session_manager, reset_session_manager
from inkscape_mcp.live.socket_backend import ENV_RENDEZVOUS, ExtensionSocketTransport
from inkscape_mcp.live.transport import LiveChange, LiveStateToken
from inkscape_mcp.resources.live import live_events
from tests.conftest import FakeTransport, mock_helper
from tests.tool_support import call

# --- token computation --------------------------------------------------------


def test_token_is_hashed_and_stable() -> None:
    raw = {"revision": "abc", "selection": ["a", "b"], "viewport": {"zoom": 1.0, "center": [2, 3]}}
    token, ids = token_from_result(raw)
    # short digests: the raw document / selection / viewport never crosses back verbatim
    assert all(
        len(c) == 16 and c.isalnum() for c in (token.revision, token.selection, token.viewport)
    )
    assert ids == ["a", "b"]
    assert token_from_result(raw)[0] == token  # nothing changed -> no spurious delta


def test_token_degrades_on_odd_components_instead_of_raising() -> None:
    raw = {"revision": 5, "selection": "not-a-list", "viewport": {"zoom": float("inf")}}
    token, ids = token_from_result(raw)
    assert ids == []
    assert token.selection == token_from_result({"selection": []})[0].selection


# --- delta classification -----------------------------------------------------


def _tok(rev: str = "r", sel: str = "s", vp: str = "v") -> LiveStateToken:
    return LiveStateToken(revision=rev, selection=sel, viewport=vp)


def test_first_observation_reports_no_change() -> None:
    change = classify_change(None, _tok(), ["a"])
    assert change.changed is False and change.selection_ids == ["a"]


@pytest.mark.parametrize(
    ("old", "new", "flags"),
    [
        (_tok(sel="old"), _tok(sel="new"), (True, False, False)),  # selection, document, viewport
        (_tok(rev="old"), _tok(rev="new"), (False, True, False)),
        (_tok(vp="old"), _tok(vp="new"), (False, False, True)),
        (_tok("a", "b", "c"), _tok("x", "y", "z"), (True, True, True)),
    ],
    ids=["selection", "document", "viewport", "all-three"],
)
def test_each_component_sets_only_its_own_flag(old, new, flags) -> None:
    change = classify_change(old, new, [])
    assert change.changed is True
    got = (change.selection_changed, change.document_changed, change.viewport_changed)
    assert got == flags


# --- manager-level detect / wait ----------------------------------------------


def _connected_manager() -> tuple[LiveSessionManager, FakeTransport]:
    transport = FakeTransport()
    transport.connect()
    mgr = LiveSessionManager(Settings(workspace_roots=[Path("/tmp")], live_enabled=True))
    mgr._transport = transport  # type: ignore[attr-defined]  # test-only direct attach
    mgr._connected_at = "now"  # type: ignore[attr-defined]
    return mgr, transport


def test_detect_change_baseline_then_each_delta_type() -> None:
    mgr, transport = _connected_manager()
    assert detect_change(mgr).changed is False  # baseline

    transport.state_selection = ["r1", "r2"]
    c = detect_change(mgr)
    assert c.selection_changed and not c.document_changed and not c.viewport_changed

    transport.state_revision = "rev-1"
    c = detect_change(mgr)
    assert c.document_changed and not c.selection_changed

    transport.state_viewport = {"zoom": 2.0, "center": [1.0, 1.0]}
    c = detect_change(mgr)
    assert c.viewport_changed and not c.document_changed


def test_wait_returns_without_sleeping_when_a_change_is_already_present() -> None:
    mgr, transport = _connected_manager()
    detect_change(mgr)
    transport.state_revision = "changed"
    slept: list[float] = []
    change = wait_for_change(timeout_s=5.0, poll_interval_s=0.01, manager=mgr, sleep=slept.append)
    assert change.changed is True and change.document_changed is True
    assert slept == []


def test_wait_times_out_with_bounded_sleeps_when_nothing_changes() -> None:
    mgr, _ = _connected_manager()
    detect_change(mgr)
    ticks = iter([0.0, 0.0, 0.05, 0.1, 0.15])
    sleeps: list[float] = []
    change = wait_for_change(
        timeout_s=0.1,
        poll_interval_s=0.02,
        manager=mgr,
        sleep=sleeps.append,
        monotonic=lambda: next(ticks, 1000.0),  # past the deadline once the script runs out
    )
    assert change.timed_out is True and change.changed is False
    assert sleeps and all(0 < s <= 0.02 for s in sleeps)  # bounded, never a busy loop


def test_wait_bounds_are_hard_capped() -> None:
    mgr, _ = _connected_manager()
    detect_change(mgr)

    def simulated_wait(timeout_s: float) -> float:
        # A fake clock that advances only by what the wait sleeps: returns the total time waited.
        now = [0.0]

        def sleep(s: float) -> None:
            now[0] += s

        change = wait_for_change(
            timeout_s=timeout_s,
            poll_interval_s=float("inf"),  # non-finite interval falls back to the default
            manager=mgr,
            sleep=sleep,
            monotonic=lambda: now[0],
        )
        assert change.timed_out is True
        return now[0]

    # NaN must not defeat the clamp (it falls back to the default) and a huge timeout is capped.
    assert 0 < simulated_wait(float("nan")) <= MAX_WAIT_TIMEOUT_S
    assert simulated_wait(10_000.0) == pytest.approx(MAX_WAIT_TIMEOUT_S)


# --- live(action=wait) --------------------------------------------------------


@pytest.fixture
def on(tool_ws, monkeypatch: pytest.MonkeyPatch) -> FakeTransport:
    """Live enabled; `connect` hands out one FakeTransport (returned so tests can mutate it)."""
    monkeypatch.setenv(ENV_LIVE_ENABLED, "1")
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tool_ws))
    get_settings.cache_clear()
    reset_session_manager()
    transport = FakeTransport()
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    monkeypatch.setattr(session_mod, "select_transport", lambda s, required: transport)
    yield transport
    reset_session_manager()


def _wait(app, timeout_s: float = 0.05, **extra):  # type: ignore[no-untyped-def]
    args = {"action": "wait", "timeout_s": timeout_s, "poll_interval_s": 0.01, **extra}
    return call(app, "live", args).data


def test_wait_connects_on_demand_and_detects_a_document_change(tool_app, on) -> None:
    assert not get_session_manager().status().connected
    first = _wait(tool_app, 0.02)  # establishes the baseline
    assert get_session_manager().status().connected
    assert first["timed_out"] is True and first["changed"] is False

    on.state_revision = "edited-by-user"
    change = _wait(tool_app, 1.0)
    assert change["changed"] is True and change["document_changed"] is True
    assert change["timed_out"] is False
    assert not change.get("selection_changed")


def test_wait_is_read_only(tool_app, on) -> None:
    _wait(tool_app, 0.02)
    on.state_revision = "z"
    _wait(tool_app, 0.5)
    assert list_live_operations(settings=get_settings()).count == 0


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"timeout_s": 999}, "timeout_s"),
        ({"timeout_s": -1}, "timeout_s"),
        ({"poll_interval_s": 0}, "poll_interval_s"),
        ({"poll_interval_s": 61}, "poll_interval_s"),
    ],
)
def test_wait_rejects_unbounded_arguments_before_connecting(tool_app, on, args, needle) -> None:
    with pytest.raises(ToolError, match=needle):
        call(tool_app, "live", {"action": "wait", **args})
    assert not get_session_manager().status().connected


# --- inkscape://live/events ---------------------------------------------------


def test_events_resource_is_empty_without_a_session(tool_ws, monkeypatch) -> None:
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tool_ws))
    get_settings.cache_clear()
    reset_session_manager()
    change = LiveChange.model_validate_json(live_events())
    assert change.changed is False and change.token == LiveStateToken()


def test_events_resource_reports_a_change_with_a_session(on) -> None:
    mgr = get_session_manager()
    mgr.connect()
    detect_change(mgr)
    on.state_revision = "user-edit"
    change = LiveChange.model_validate_json(live_events())
    assert change.changed is True and change.document_changed is True


# --- real socket client -------------------------------------------------------


def test_get_state_token_roundtrips_over_the_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    seen: dict[str, dict] = {}

    def handler(cmd: str, params: dict) -> dict:
        if cmd == LiveCommand.GET_STATE_TOKEN:
            seen["params"] = params
            return {
                "revision": "rev-9",
                "selection": ["a", "b"],
                "viewport": {"zoom": 1.0, "center": [2.0, 2.0]},
            }
        raise KeyError(cmd)

    secret = "secret-token"
    with mock_helper(token=secret, handler=handler) as port:
        rv = tmp_path / "rv.json"
        rv.write_text(
            json.dumps({"port": port, "token": secret, "protocol_version": PROTOCOL_VERSION}),
            encoding="utf-8",
        )
        rv.chmod(0o600)  # discovery only trusts a private, own-user rendezvous
        monkeypatch.setenv(ENV_RENDEZVOUS, str(rv))
        transport = ExtensionSocketTransport(get_settings())
        transport.connect()
        token, ids = transport.get_state_token()
        transport.disconnect()
    assert ids == ["a", "b"]
    assert all(len(c) == 16 for c in (token.revision, token.selection, token.viewport))
    assert seen["params"] == {}  # takes no params; the payload carries no document or PNG
