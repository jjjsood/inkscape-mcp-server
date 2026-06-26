"""Live session state + manager.

Holds the process-wide live session: which transport (if any) is connected, when, and the active
document identity. The live gate is ON by default (X1, operator decision 2026-06-14) — the master
gate (`settings.live_enabled`) must be on for `connect` to attach (set the env falsy to opt out),
and `connect`/`disconnect` are the per-session enable/disable toggle.
Nothing here runs unless a tool explicitly asks; headless is wholly independent of this module.

A connected transport is held privately so the read tools and sync/render can use
it; the public `LiveSession` model never exposes the live socket or any host-internal detail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from threading import Lock

from pydantic import BaseModel, Field

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.live.cache import RenderCache
from inkscape_mcp.live.records import clear_live_operations
from inkscape_mcp.live.selection import (
    NO_FREEZE_REQUIRED,
    READ_REQUIRED,
    probe_transports,
    select_transport,
)
from inkscape_mcp.live.transport import (
    LiveChange,
    LiveDisabled,
    LiveDocumentRef,
    LiveError,
    LiveNotAvailable,
    LiveStateToken,
    LiveTransport,
)
from inkscape_mcp.logging_setup import get_logger

_logger = get_logger("live.session")

#: Stable identifier of the extension-socket transport (`ExtensionSocketTransport.name`). Held as a
#: local constant so `status()` can reconcile `available_transports` (R10) WITHOUT importing the
#: socket backend module directly — keeping this module's import graph minimal. It must stay in
#: lockstep with the backend's `name` class attribute.
_EXTENSION_SOCKET_NAME = "extension-socket"


class LiveSession(BaseModel):
    """Public, read-only snapshot of live-session state (tool output + session resource)."""

    enabled: bool = Field(description="Whether live mode is permitted (master gate, X1).")
    connected: bool = Field(description="Whether a live transport is currently attached.")
    transport: str | None = Field(default=None, description="Active transport name, if connected.")
    available_transports: list[str] = Field(
        default_factory=list, description="Transports reported available on this host right now."
    )
    active_document: LiveDocumentRef | None = Field(
        default=None, description="Identity of the live document at connect time."
    )
    connected_at: str | None = Field(default=None, description="UTC ISO-8601 connect timestamp.")
    notes: list[str] = Field(default_factory=list, description="Clean human-readable status notes.")


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _extension_socket_installed() -> bool:
    """Best-effort: whether the runtime probe reports the extension-socket helper installed (R10).

    Mirrors the availability notion the capability probe exposes as
    `live_extension_socket_available`, so the session resource's `available_transports` agrees with
    `diagnose_runtime`. Best-effort — any probe failure means "unknown", reported as not installed,
    and never raises into `status()`.
    """
    try:
        from inkscape_mcp.tools.system import get_cached_capabilities

        return bool(get_cached_capabilities().live_extension_socket_available)
    except Exception:  # pragma: no cover - probe best-effort; absence just means "unknown"
        return False


class LiveSessionManager:
    """Process-wide holder of the connected transport + session model (thread-safe)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._lock = Lock()
        self._transport: LiveTransport | None = None
        self._connected_at: str | None = None
        self._active_document: LiveDocumentRef | None = None
        #: Last state token + last classified change from the change-detection layer. Held
        #: across calls so `live_wait_for_change` and the events resource share a baseline; reset on
        #: every connect/disconnect so a stale token can never survive a new session.
        self._last_token: LiveStateToken | None = None
        self._last_change: LiveChange | None = None
        #: Per-session bounded render-frame cache + coalescing state. Built on connect from
        #: the settings bounds and reset on every connect/disconnect so a frame from one session can
        #: never leak into another. None while disconnected.
        self._render_cache: RenderCache | None = None

    # --- queries ---

    def status(self) -> LiveSession:
        """Return the current `LiveSession` snapshot (never raises)."""
        with self._lock:
            probes = probe_transports(self._settings)
            available = [p.name for p in probes if p.available]
            # R10 reconciliation: `probe_transports` marks the extension-socket transport
            # `available=False` until a live session is advertising a rendezvous socket, but the
            # runtime capability probe reports it AVAILABLE once the helper is installed. Surface
            # that installed-but-no-session transport here too, so `available_transports` agrees
            # with `diagnose_runtime`'s `live_extension_socket_available` (the socket bridge is the
            # cross-platform primary per the live-mode rule and must not be hidden from the list).
            if _extension_socket_installed() and _EXTENSION_SOCKET_NAME not in available:
                available.append(_EXTENSION_SOCKET_NAME)
            connected = self._transport is not None and self._transport.is_connected()
            notes: list[str] = []
            if not self._settings.live_enabled:
                notes.append(
                    "live mode is disabled by configuration (set INKSCAPE_MCP_LIVE_ENABLED)"
                )
            if not available:
                notes.append("no live transport available on this host")
            return LiveSession(
                enabled=self._settings.live_enabled,
                connected=connected,
                transport=self._transport.name if connected and self._transport else None,
                available_transports=available,
                active_document=self._active_document if connected else None,
                connected_at=self._connected_at if connected else None,
                notes=notes,
            )

    def require_transport(self) -> LiveTransport:
        """Return the connected transport or raise `LiveNotAvailable` (read-tool precondition)."""
        with self._lock:
            if self._transport is None or not self._transport.is_connected():
                raise LiveNotAvailable("no live session connected")
            return self._transport

    # --- change-detection state ---

    def last_change_state(self) -> tuple[LiveStateToken | None, LiveChange | None]:
        """Return the last observed (token, change) pair (None,None before the first poll)."""
        with self._lock:
            return self._last_token, self._last_change

    def record_change_state(self, token: LiveStateToken, change: LiveChange) -> None:
        """Store the newest observed token + classified change (set by the events layer)."""
        with self._lock:
            self._last_token = token
            self._last_change = change

    # --- render cache ---

    def render_cache(self) -> RenderCache | None:
        """Return the per-session render cache, or None when no session is connected.

        Built on connect from the settings bounds and reset on connect/disconnect, so a frame from
        one session can never be served in another. View-only — holds workspace-relative artifact
        paths, never document state or a host path.
        """
        with self._lock:
            return self._render_cache

    # --- lifecycle ---

    def connect(self, prefer: str = "read") -> LiveSession:
        """Select + connect the best transport, capturing the active document (X1 enable).

        `prefer` chooses the connection profile:

        * ``"read"`` (default) — select the best READ-capable transport (extension-socket primary;
          full selection/inspect surface, but modal on every OS).
        * ``"no_freeze"`` — select a transport that drives the GUI WITHOUT freezing it (the Linux
          DBus action path). The export-based active-document read, viewport, and
          style/transform writes are no-freeze; selection-id reads are unavailable over this path
          (honest trade-off). Falls back cleanly to "no transport" on Windows/macOS or when no DBus
          instance is present.

        Refuses with `LiveDisabled` when the master gate is off, or with `ValueError` on an unknown
        `prefer`. Returns the post-connect `LiveSession`. On any failure the manager is left cleanly
        disconnected so a live fault never leaves a half-open session.
        """
        if not self._settings.live_enabled:
            raise LiveDisabled("live mode is disabled; enable it with INKSCAPE_MCP_LIVE_ENABLED=1")
        if prefer not in ("read", "no_freeze"):
            raise ValueError("prefer must be 'read' or 'no_freeze'")
        with self._lock:
            self._teardown_locked()
            if prefer == "no_freeze":
                transport = select_transport(self._settings, NO_FREEZE_REQUIRED, no_freeze=True)
                if transport is None:
                    raise LiveNotAvailable(
                        "no no-freeze live transport available (the Linux DBus action path "
                        "requires a running Inkscape on the session bus)"
                    )
            else:
                transport = select_transport(self._settings, READ_REQUIRED)
                if transport is None:
                    raise LiveNotAvailable(
                        "no live transport supports the required read operations"
                    )
            try:
                transport.connect()
                active_document = transport.get_active_document()
            except LiveError:
                transport.disconnect()
                raise
            self._transport = transport
            self._connected_at = _utc_iso()
            self._active_document = active_document
            self._render_cache = RenderCache(
                max_entries=self._settings.live_cache_max_entries,
                max_bytes=self._settings.live_cache_max_bytes,
                coalesce_budget_ms=self._settings.live_coalesce_budget_ms,
            )
            _logger.info(
                "live session opened",
                extra={"event": "process_exec", "transport": transport.name},
            )
        return self.status()

    def disconnect(self) -> LiveSession:
        """Disconnect any live session (idempotent). Returns the post-disconnect `LiveSession`."""
        with self._lock:
            self._teardown_locked()
            _logger.info("live session closed", extra={"event": "process_exec"})
        return self.status()

    def _teardown_locked(self) -> None:
        if self._transport is not None:
            self._transport.disconnect()
        self._transport = None
        self._connected_at = None
        self._active_document = None
        self._last_token = None
        self._last_change = None
        if self._render_cache is not None:
            self._render_cache.clear()
        self._render_cache = None
        # Scope live op state to the session: clear persisted Live Operation Records on every
        # connect/disconnect boundary so a stale prior-session record (potentially carrying a host
        # path before sanitization) can never surface in the `live/operations` resource of a
        # later session. Best-effort — clearing must never break the lifecycle.
        try:
            clear_live_operations(self._settings)
        except Exception:  # pragma: no cover - defensive; clearing is non-critical
            _logger.warning("live operation-record clear failed", extra={"event": "file_io"})


_manager: LiveSessionManager | None = None
_manager_lock = Lock()


def get_session_manager() -> LiveSessionManager:
    """Return the process-wide `LiveSessionManager` singleton."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = LiveSessionManager()
        return _manager


def reset_session_manager() -> None:
    """Drop the singleton (test helper); disconnects any open session first."""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.disconnect()
        _manager = None
