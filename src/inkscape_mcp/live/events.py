"""Change detection + bounded change-wait (E8-03, ADR-006, low risk).

The mechanism that lets the live loop render ONLY when something actually changes — including the
user's own GUI edits — instead of busy-rendering. Each poll pulls a CHEAP ``get_state_token`` over
the fixed transport schema (protocol v5): a small revision marker + selection ids + a coarse
viewport, NEVER the full document or a PNG. The server hashes those raw components into a stable
`LiveStateToken` and classifies any delta against the last seen token as
``selection_changed`` / ``document_changed`` / ``viewport_changed`` (more than one may fire).

``live_wait_for_change`` is a BOUNDED, cancelable poll: it sleeps a sane interval between cheap
token reads up to a capped ``timeout_s`` and returns the instant a delta is detected, or a
``timed_out`` `LiveChange` when the budget elapses. It NEVER spins tightly and NEVER blocks
unbounded (hard requirement — pairs with the E8-06 latency budget).

READ-ONLY: detecting a change mutates nothing in the live document and produces NO Operation
Record (it mirrors ``render_live_view`` / ``get_scene``). All token components are coerced/bounded
server-side; the wire message is size-capped by the transport. There is no code/raw-Action path
(ADR-003).

KNOWN HELPER LIMITATION: the shipped extension-socket helper is an ``inkex`` *effect* extension —
it runs on the document snapshot Inkscape hands it at invocation, so within a single invocation it
cannot observe subsequent live GUI edits (its revision marker / selection / viewport reflect the
snapshot). The TOKEN MECHANISM is correct and transport-agnostic: a transport that recomputes from
the *current* document on each poll (a DBus/live fast-path, or a future persistent bridge) detects
the user's manual edits with no change here — the design is not instance- or OS-specific.
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Any

from inkscape_mcp.live.session import LiveSessionManager, get_session_manager
from inkscape_mcp.live.transport import LiveChange, LiveStateToken, LiveTransport
from inkscape_mcp.logging_setup import get_logger, log_event

_logger = get_logger("live.events")

#: Default poll interval (seconds) between cheap token reads. Sane low-latency default that does not
#: hammer the transport; overridable per call (tests inject a tiny value to stay fast).
DEFAULT_POLL_INTERVAL_S = 0.5

#: Hard floor on the poll interval — a non-positive/too-small value can never turn the wait into a
#: tight busy-loop that hammers the transport.
MIN_POLL_INTERVAL_S = 0.01

#: Default and HARD-CAP bound on a single change-wait (seconds). The wait is always bounded: a
#: caller-supplied timeout is clamped into ``[0, MAX_WAIT_TIMEOUT_S]`` so it never blocks forever.
DEFAULT_WAIT_TIMEOUT_S = 5.0
MAX_WAIT_TIMEOUT_S = 60.0

#: Upper bound on selection ids folded into the selection digest from one token payload. Bounds the
#: work a hostile/runaway helper can force even within the transport's 64 MiB frame cap.
_MAX_SELECTION_IDS = 10_000


def _digest(text: str) -> str:
    """Stable short hex digest of a string (server-side; the raw value never crosses back)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _opt_number(value: Any) -> float | None:
    """Coerce a JSON number to a finite float, or None (rejects NaN/inf, bools, non-numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    num = float(value)
    return num if math.isfinite(num) else None


def _revision_digest(raw: Any) -> str:
    """Digest the document revision marker (a string/number the helper supplies, e.g. a content or
    undo-counter hash). A missing/odd value digests to a stable empty marker rather than raising."""
    if isinstance(raw, str):
        return _digest(raw)
    num = _opt_number(raw)
    if num is not None:
        return _digest(repr(num))
    return _digest("")


def _selection_digest(raw: Any) -> str:
    """Digest the ORDERED selection ids (order matters: a reordered selection is a change)."""
    if not isinstance(raw, list):
        return _digest("")
    ids = [str(i) for i in raw[:_MAX_SELECTION_IDS] if isinstance(i, str)]
    return _digest("\x1f".join(ids))


def _viewport_digest(raw: Any) -> str:
    """Digest a COARSE viewport: zoom + center (x, y). Non-finite/odd fields drop to a stable
    placeholder so a buggy peer cannot inject untyped data into the digest input."""
    if not isinstance(raw, dict):
        return _digest("")
    zoom = _opt_number(raw.get("zoom"))
    center = raw.get("center")
    cx = cy = None
    if isinstance(center, (list, tuple)) and len(center) == 2:
        cx, cy = _opt_number(center[0]), _opt_number(center[1])
    parts = "|".join("" if v is None else repr(round(v, 4)) for v in (zoom, cx, cy))
    return _digest(parts)


def _selection_ids(raw: Any) -> list[str]:
    """Coerce the raw selection-id list for the change result (bounded, strings only)."""
    if not isinstance(raw, list):
        return []
    return [str(i) for i in raw[:_MAX_SELECTION_IDS] if isinstance(i, str)]


def token_from_result(result: dict[str, Any]) -> tuple[LiveStateToken, list[str]]:
    """Hash a raw ``get_state_token`` wire result into a `LiveStateToken` (+ selection ids).

    Every component is coerced/bounded server-side and hashed; an absent or malformed field degrades
    to a stable empty digest rather than raising, so a buggy peer can never inject untyped data into
    the token. Returns the token plus the (coerced) selection ids for the `LiveChange` convenience.
    """
    token = LiveStateToken(
        revision=_revision_digest(result.get("revision")),
        selection=_selection_digest(result.get("selection")),
        viewport=_viewport_digest(result.get("viewport")),
    )
    return token, _selection_ids(result.get("selection"))


def classify_change(
    previous: LiveStateToken | None,
    current: LiveStateToken,
    selection_ids: list[str],
) -> LiveChange:
    """Classify the delta between `previous` and `current` into a typed `LiveChange`.

    The FIRST observation (no previous token) reports no change — there is nothing to diff against —
    so a fresh poll never spuriously fires. Each component is compared independently, so more than
    one flag can fire at once.
    """
    if previous is None:
        return LiveChange(token=current, selection_ids=selection_ids)
    selection_changed = previous.selection != current.selection
    document_changed = previous.revision != current.revision
    viewport_changed = previous.viewport != current.viewport
    return LiveChange(
        changed=selection_changed or document_changed or viewport_changed,
        selection_changed=selection_changed,
        document_changed=document_changed,
        viewport_changed=viewport_changed,
        token=current,
        selection_ids=selection_ids,
    )


def get_state_token(manager: LiveSessionManager | None = None) -> tuple[LiveStateToken, list[str]]:
    """Pull + hash one cheap `LiveStateToken` from the connected transport (read-only).

    Requires an established session (raises `LiveNotAvailable` via ``require_transport`` otherwise)
    and a transport that serves ``get_state_token`` (raises `LiveCapabilityUnsupported` otherwise).
    Never serializes the full document or a PNG; never mutates the live document.
    """
    mgr = manager if manager is not None else get_session_manager()
    transport: LiveTransport = mgr.require_transport()
    return transport.get_state_token()


def detect_change(manager: LiveSessionManager | None = None) -> LiveChange:
    """Pull one token, classify it against the session's last-seen token, and persist the result.

    A single non-blocking observation (used by the events resource and as the wait loop's step). The
    new token + classified change are recorded on the session so the next call / the events resource
    share the same baseline. Read-only — no document mutation, no Operation Record.
    """
    mgr = manager if manager is not None else get_session_manager()
    token, selection_ids = get_state_token(mgr)
    previous, _ = mgr.last_change_state()
    change = classify_change(previous, token, selection_ids)
    mgr.record_change_state(token, change)
    if change.changed:
        log_event(
            _logger,
            "preview",
            live_event="change_detected",
            selection_changed=change.selection_changed,
            document_changed=change.document_changed,
            viewport_changed=change.viewport_changed,
        )
    return change


def wait_for_change(
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    manager: LiveSessionManager | None = None,
    sleep: Any = time.sleep,
    monotonic: Any = time.monotonic,
) -> LiveChange:
    """Bounded, cancelable poll for the next live change (E8-03; read-only, no busy-loop).

    Reads the cheap token, returns immediately on a detected delta, otherwise sleeps
    ``poll_interval_s`` and retries until ``timeout_s`` elapses — then returns a ``timed_out``
    `LiveChange`. The wait is ALWAYS bounded: ``timeout_s`` is clamped into
    ``[0, MAX_WAIT_TIMEOUT_S]`` and the interval is floored at ``MIN_POLL_INTERVAL_S`` so it can
    never spin tightly or block forever. ``sleep`` / ``monotonic`` are injectable for fast tests.
    Requires an established session. Read-only — no document mutation, no Operation Record.
    """
    mgr = manager if manager is not None else get_session_manager()
    # Coerce non-finite inputs (NaN/inf) to safe defaults FIRST — otherwise a NaN would defeat the
    # clamp below (`max(0.0, nan)` is nan) and make the wait unbounded.
    raw_timeout = float(timeout_s)
    if not math.isfinite(raw_timeout):
        raw_timeout = DEFAULT_WAIT_TIMEOUT_S
    raw_interval = float(poll_interval_s)
    if not math.isfinite(raw_interval):
        raw_interval = DEFAULT_POLL_INTERVAL_S
    timeout = max(0.0, min(raw_timeout, MAX_WAIT_TIMEOUT_S))
    interval = max(MIN_POLL_INTERVAL_S, raw_interval)
    deadline = monotonic() + timeout

    # First observation establishes / refreshes the baseline and may already detect a change vs the
    # session's last-seen token.
    change = detect_change(mgr)
    if change.changed:
        return change

    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(interval, remaining))
        change = detect_change(mgr)
        if change.changed:
            return change

    # Bounded timeout: report the latest token, no change, timed_out=True.
    last_token, _ = mgr.last_change_state()
    return LiveChange(
        changed=False,
        timed_out=True,
        token=last_token if last_token is not None else change.token,
    )
