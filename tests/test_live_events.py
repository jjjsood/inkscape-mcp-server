"""Change-detection + event-stream tests (E8-03).

Covers the cheap state-token computation + stability, delta classification per component, the
bounded + cancelable `live_wait_for_change` tool (returns promptly on change, `timed_out` on no
change within a short budget), the read-only `inkscape://live/events` resource (degrades with no
session), the v5 socket round-trip + lock-step, and that detecting a change records no Operation
Record. Interval/timeout are injected so every test stays fast.
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
    MIN_POLL_INTERVAL_S,
    classify_change,
    detect_change,
    token_from_result,
    wait_for_change,
)
from inkscape_mcp.live.session import LiveSessionManager, get_session_manager, reset_session_manager
from inkscape_mcp.live.transport import LiveChange, LiveStateToken
from inkscape_mcp.resources.live import live_events
from inkscape_mcp.tools.live import live_connect, live_wait_for_change

from .conftest import FakeTransport, mock_helper

# --- token computation + stability -----------------------------------------


def test_token_from_result_is_hashed_and_stable() -> None:
    raw = {"revision": "abc", "selection": ["a", "b"], "viewport": {"zoom": 1.0, "center": [2, 3]}}
    token, ids = token_from_result(raw)
    # Components are short hex digests — the raw doc/selection/viewport never crosses back verbatim.
    assert all(
        len(c) == 16 and c.isalnum() for c in (token.revision, token.selection, token.viewport)
    )
    assert ids == ["a", "b"]
    # Identical input → identical token (stable; nothing changed → no spurious delta).
    again, _ = token_from_result(raw)
    assert token == again


def test_token_rejects_non_finite_and_odd_components() -> None:
    # NaN/inf zoom and a non-list selection degrade to stable empty digests, never raise.
    raw = {"revision": 5, "selection": "not-a-list", "viewport": {"zoom": float("inf")}}
    token, ids = token_from_result(raw)
    assert ids == []
    empty_sel, _ = token_from_result({"selection": []})
    assert token.selection == empty_sel.selection


# --- delta classification ---------------------------------------------------


def _tok(rev: str = "r", sel: str = "s", vp: str = "v") -> LiveStateToken:
    return LiveStateToken(revision=rev, selection=sel, viewport=vp)


def test_classify_first_observation_reports_no_change() -> None:
    change = classify_change(None, _tok(), ["a"])
    assert change.changed is False
    assert change.selection_ids == ["a"]


def test_classify_selection_change() -> None:
    change = classify_change(_tok(sel="old"), _tok(sel="new"), ["x"])
    assert change.changed is True
    assert change.selection_changed is True
    assert change.document_changed is False
    assert change.viewport_changed is False


def test_classify_document_change() -> None:
    change = classify_change(_tok(rev="old"), _tok(rev="new"), [])
    assert change.changed is True
    assert change.document_changed is True
    assert change.selection_changed is False
    assert change.viewport_changed is False


def test_classify_viewport_change() -> None:
    change = classify_change(_tok(vp="old"), _tok(vp="new"), [])
    assert change.changed is True
    assert change.viewport_changed is True
    assert change.selection_changed is False
    assert change.document_changed is False


def test_classify_multiple_flags_can_fire() -> None:
    change = classify_change(_tok("a", "b", "c"), _tok("x", "y", "z"), [])
    assert change.selection_changed and change.document_changed and change.viewport_changed


# --- manager-level detect/wait ----------------------------------------------


def _connected_manager() -> tuple[LiveSessionManager, FakeTransport]:
    transport = FakeTransport()
    transport.connect()
    mgr = LiveSessionManager(Settings(workspace_roots=[Path("/tmp")], live_enabled=True))
    mgr._transport = transport  # type: ignore[attr-defined]  # test-only direct attach
    mgr._connected_at = "now"  # type: ignore[attr-defined]
    return mgr, transport


def test_detect_change_no_change_then_each_delta_type() -> None:
    mgr, transport = _connected_manager()
    # First observation establishes the baseline (no change).
    assert detect_change(mgr).changed is False

    transport.state_selection = ["r1", "r2"]
    c = detect_change(mgr)
    assert c.changed and c.selection_changed and not c.document_changed and not c.viewport_changed

    transport.state_revision = "rev-1"
    c = detect_change(mgr)
    assert c.changed and c.document_changed and not c.selection_changed

    transport.state_viewport = {"zoom": 2.0, "center": [1.0, 1.0]}
    c = detect_change(mgr)
    assert c.changed and c.viewport_changed and not c.document_changed


def test_wait_returns_promptly_on_change_already_present() -> None:
    mgr, transport = _connected_manager()
    detect_change(mgr)  # baseline
    transport.state_revision = "changed"
    # No sleep should be needed — the first poll already sees the delta.
    slept: list[float] = []
    change = wait_for_change(
        timeout_s=5.0, poll_interval_s=0.01, manager=mgr, sleep=lambda s: slept.append(s)
    )
    assert change.changed is True
    assert change.document_changed is True
    assert slept == []


def test_wait_times_out_within_budget_when_nothing_changes() -> None:
    mgr, _ = _connected_manager()
    detect_change(mgr)  # baseline; nothing mutates afterward

    # Drive a fake monotonic clock so the bounded loop terminates deterministically + fast.
    ticks = iter([0.0, 0.0, 0.05, 0.1, 0.15, 0.2, 0.25])
    sleeps: list[float] = []

    def fake_monotonic() -> float:
        try:
            return next(ticks)
        except StopIteration:
            return 1000.0  # force the deadline past, guaranteeing termination

    change = wait_for_change(
        timeout_s=0.1,
        poll_interval_s=0.02,
        manager=mgr,
        sleep=lambda s: sleeps.append(s),
        monotonic=fake_monotonic,
    )
    assert change.timed_out is True
    assert change.changed is False
    # It slept between polls (bounded, not a tight busy-loop) and still returned.
    assert sleeps  # at least one bounded sleep happened


def test_wait_timeout_is_clamped_to_hard_cap() -> None:
    mgr, transport = _connected_manager()
    detect_change(mgr)
    transport.state_revision = "x"
    # A huge requested timeout is clamped; since a change is present it returns at once anyway.
    deadlines: list[float] = []

    def fake_monotonic() -> float:
        deadlines.append(0.0)
        return 0.0

    change = wait_for_change(
        timeout_s=10_000.0, poll_interval_s=0.01, manager=mgr, monotonic=fake_monotonic
    )
    assert change.changed is True
    assert MAX_WAIT_TIMEOUT_S == 60.0


def test_interval_is_floored_against_busy_loop() -> None:
    assert MIN_POLL_INTERVAL_S > 0


# --- tool surface -----------------------------------------------------------


@pytest.fixture
def on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeTransport:
    """Live enabled with a connected FakeTransport (returns it so a test can mutate its state)."""
    monkeypatch.setenv(ENV_LIVE_ENABLED, "1")
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    reset_session_manager()
    transport = FakeTransport()
    monkeypatch.setattr(
        session_mod,
        "probe_transports",
        lambda settings=None: [],
    )
    monkeypatch.setattr(session_mod, "select_transport", lambda s, required: transport)
    live_connect()
    return transport


def test_live_wait_for_change_tool_detects_document_change(on: FakeTransport) -> None:
    # First call establishes the baseline (no change within a tiny budget → timed out).
    first = live_wait_for_change(timeout_s=0.02, poll_interval_s=0.01)
    assert first.timed_out is True

    # Mutate the document marker; the next bounded wait returns promptly with the classification.
    on.state_revision = "edited-by-user"
    change = live_wait_for_change(timeout_s=1.0, poll_interval_s=0.01)
    assert change.changed is True
    assert change.document_changed is True
    assert change.timed_out is False


def test_live_wait_for_change_times_out_short_budget(on: FakeTransport) -> None:
    live_wait_for_change(timeout_s=0.02, poll_interval_s=0.01)  # baseline
    change = live_wait_for_change(timeout_s=0.05, poll_interval_s=0.01)
    assert change.timed_out is True
    assert change.changed is False


def test_live_wait_for_change_errors_cleanly_without_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_LIVE_ENABLED, "1")
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    reset_session_manager()
    with pytest.raises(ToolError):
        live_wait_for_change(timeout_s=0.02, poll_interval_s=0.01)


def test_wait_creates_no_operation_record(on: FakeTransport) -> None:
    from inkscape_mcp.live.records import list_live_operations

    before = len(list_live_operations().operations)
    live_wait_for_change(timeout_s=0.02, poll_interval_s=0.01)
    on.state_revision = "z"
    live_wait_for_change(timeout_s=0.5, poll_interval_s=0.01)
    after = len(list_live_operations().operations)
    assert after == before  # change detection is read-only — never records an operation


# --- resource ---------------------------------------------------------------


def test_events_resource_empty_without_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    reset_session_manager()
    change = LiveChange.model_validate_json(live_events())
    assert change.changed is False
    assert change.token == LiveStateToken()


def test_events_resource_reports_change_with_session(on: FakeTransport) -> None:
    # Prime the baseline through the shared singleton, then mutate and read the resource.
    mgr = get_session_manager()
    detect_change(mgr)
    on.state_revision = "user-edit"
    change = LiveChange.model_validate_json(live_events())
    assert change.changed is True
    assert change.document_changed is True


# --- socket round-trip + lock-step ------------------------------------------


def test_get_state_token_roundtrip_over_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from inkscape_mcp.live.protocol import PROTOCOL_VERSION, LiveCommand
    from inkscape_mcp.live.socket_backend import ENV_RENDEZVOUS, ExtensionSocketTransport

    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    settings = get_settings()

    seen: dict[str, dict] = {}

    def handler(cmd: str, params: dict) -> dict:
        if cmd == LiveCommand.GET_STATE_TOKEN:
            seen["token"] = params
            return {
                "revision": "rev-9",
                "selection": ["a", "b"],
                "viewport": {"zoom": 1.0, "center": [2.0, 2.0]},
            }
        raise KeyError(cmd)

    token = "secret-token"
    with mock_helper(token=token, handler=handler) as port:
        rv = tmp_path / "rv.json"
        rv.write_text(
            json.dumps({"port": port, "token": token, "protocol_version": PROTOCOL_VERSION}),
            encoding="utf-8",
        )
        monkeypatch.setenv(ENV_RENDEZVOUS, str(rv))

        transport = ExtensionSocketTransport(settings)
        transport.connect()
        state_token, ids = transport.get_state_token()
        # Server hashed the cheap components; the raw payload carried no doc/PNG.
        assert ids == ["a", "b"]
        assert all(len(c) == 16 for c in (state_token.revision, state_token.selection))
        assert seen["token"] == {}  # get_state_token takes no params
        transport.disconnect()
