"""Live tools (05/06) — detect, connect, read, render, sync a running Inkscape.

Thin `@mcp.tool` layer over the live package (`inkscape_mcp.live.*`). Every tool is constrained to
the fixed semantic surface — no arbitrary code, no raw Action strings (ADR-003). The live gate is
ON by default (X1, operator decision 2026-06-14; set the env falsy to opt out):
`check_live_support`/`live_status` always answer (read-only), but connecting and reading require the
master gate on plus an established session, and absence is reported cleanly rather than as an error
path. Headless tools are wholly unaffected when live is unavailable.

Client-facing failures are raised as `ToolError` with stable, host-path-free messages (sec.12).
"""

from __future__ import annotations

import contextlib
import platform
from pathlib import Path
from typing import Annotated

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from inkscape_mcp.config import get_settings
from inkscape_mcp.edit.dom import EditError
from inkscape_mcp.live.diff import LiveDiffError, LiveDiffResult, diff_live_operation
from inkscape_mcp.live.edit import (
    FAST_RENDER_SCALE,
    LiveEditResult,
    LiveExportResult,
    build_style,
    build_transform,
    export_live_selection,
    run_live_mutation,
    set_live_viewport,
    validate_region,
    validate_render_scale,
    validate_svg_fragment,
    validate_text,
    validate_viewport,
)
from inkscape_mcp.live.events import (
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_WAIT_TIMEOUT_S,
    wait_for_change,
)
from inkscape_mcp.live.loop import LiveSessionStepResult, StepAction, run_session_step
from inkscape_mcp.live.protocol import LiveCommand
from inkscape_mcp.live.render import LiveRenderResult, render_live_view
from inkscape_mcp.live.scene import get_live_scene
from inkscape_mcp.live.selection import READ_REQUIRED, best_available, probe_transports
from inkscape_mcp.live.session import LiveSession, get_session_manager
from inkscape_mcp.live.socket_backend import SocketArmError, arm_socket_helper, is_helper_installed
from inkscape_mcp.live.sync import LiveSyncError, LiveSyncResult, sync_live_to_workspace
from inkscape_mcp.live.transport import (
    BBox,
    LiveCapabilityUnsupported,
    LiveChange,
    LiveConnectionError,
    LiveDisabled,
    LiveDocumentRef,
    LiveError,
    LiveMutationResult,
    LiveNotAvailable,
    LiveScene,
    LiveSelection,
    LiveSelectionInspection,
    LiveTransport,
    LiveViewportResult,
    SceneSelectionItem,
    TransportProbe,
)
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.server import mcp
from inkscape_mcp.workspace.risk import PolicyViolation

_logger = get_logger("tools.live")


class LiveSupport(BaseModel):
    """Per-host live capability report (`check_live_support`).

    Lists EVERY transport probed on this host (not one assumed by OS) with its availability, plus
    the best read-capable transport and whether the helper extension is installed.
    """

    platform: str = Field(description="OS platform string (linux/darwin/windows).")
    live_enabled: bool = Field(description="Master gate state (X1; default on).")
    any_available: bool = Field(description="Whether any transport is available right now.")
    best_transport: str | None = Field(
        default=None, description="Best read-capable available transport, or null."
    )
    helper_installed: bool = Field(
        description="Whether the extension-socket helper is installed under a data dir."
    )
    transports: list[TransportProbe] = Field(
        default_factory=list, description="Per-transport probe results, ranked best-first."
    )
    notes: list[str] = Field(default_factory=list)


class HelperInstallResult(BaseModel):
    """Outcome of `live_install_helper`: where the helper landed and which files were written.

    `extensions_dir` is presented in a `~`-relative (non-absolute) form so the result is friendly
    and never carries a full home path (L5).
    """

    installed_files: list[str]
    extensions_dir: str = Field(
        description="Install dir, `~`-relative (never an absolute host path)."
    )


class SocketArmResult(BaseModel):
    """Outcome of `live_arm_socket`: whether the socket helper is armed and serving.

    `armed` is True iff a live Inkscape is now advertising the extension-socket rendezvous (the FULL
    perceive/compose command set is reachable). `helper_installed` reflects whether the helper files
    are present, `launched` whether THIS call started a new headful Inkscape (False when an existing
    armed session was reused). `transport` is the socket transport name. No host path is carried.
    """

    armed: bool
    launched: bool
    helper_installed: bool
    transport: str = "extension-socket"
    notes: list[str] = Field(default_factory=list)


class LiveSceneFrame(BaseModel):
    """One captured live frame: the rendered PNG paired with the structured `LiveScene`.

    The decisive "better than inkmcp" capture: a frame is never just pixels — it always carries the
    machine-readable scene (selection ids + bboxes, viewport, canvas size, visible-object summary)
    so the agent reasons over structure. Read-only — no document mutation, no Operation Record.
    """

    render: LiveRenderResult = Field(description="Rendered frame (workspace-relative PNG path).")
    scene: LiveScene = Field(description="Structured scene paired with this frame (read-only).")


def _validate_frame_params(
    region_x: float | None,
    region_y: float | None,
    region_width: float | None,
    region_height: float | None,
    scale: float | None,
    fast: bool,
) -> tuple[object | None, float | None]:
    """Validate the shared region/scale/fast frame params (+).

    Returns the validated ``(region, scale)``. All four region parts are required together.
    ``fast`` applies the documented loop downscale (`FAST_RENDER_SCALE`) when no explicit ``scale``
    is given — an explicit ``scale`` always wins (full-res on demand). Raises `EditError` on a
    partial region or an out-of-bounds scale.
    """
    region_parts = (region_x, region_y, region_width, region_height)
    supplied = [p is not None for p in region_parts]
    if any(supplied) and not all(supplied):
        raise EditError("region requires all of region_x, region_y, region_width, region_height")
    region = (
        validate_region(region_x, region_y, region_width, region_height)  # type: ignore[arg-type]
        if all(supplied)
        else None
    )
    effective_scale = scale if scale is not None else (FAST_RENDER_SCALE if fast else None)
    checked_scale = validate_render_scale(effective_scale) if effective_scale is not None else None
    return region, checked_scale


def _present_extensions_dir(extensions_dir: Path) -> str:
    """Render the Inkscape extensions dir in a `~`-relative (non-absolute) form (L5).

    The helper lands under the running user's own Inkscape data dir (which they control), but the
    raw value is an absolute host path. Collapse the home prefix to `~` so the returned location is
    friendlier and carries no full home path; a dir outside the home falls back to its basename
    rather than an absolute path.
    """
    try:
        home = Path.home()
    except (RuntimeError, OSError):  # pragma: no cover - home undiscoverable
        return extensions_dir.name
    try:
        return (Path("~") / extensions_dir.relative_to(home)).as_posix()
    except ValueError:
        # Outside the home dir: present just the trailing components, never the absolute path.
        return extensions_dir.name


def _no_extensions_dir_message() -> str:
    """Stable, host-path-free message when the Inkscape extensions dir is undeterminable.

    This is a capability ABSENCE (no Inkscape data dir on this runtime), so it names the discovery
    tools so the agent can inspect what this runtime supports rather than retry blindly.
    """
    return (
        "could not determine the Inkscape extensions directory; "
        "call list_capabilities to see what this runtime supports"
    )


def _map_live_error(exc: Exception) -> ToolError:
    """Map a live-layer exception to a stable, host-path-free `ToolError`."""
    if isinstance(exc, PolicyViolation):
        # Approval gate message, built from the policy layer — already safe (no host path).
        return ToolError(str(exc))
    if isinstance(exc, EditError):
        # Validation message, built from typed parameters — already safe (no host path).
        return ToolError(str(exc))
    if isinstance(exc, LiveDisabled):
        # CAPABILITY-ABSENT: live mode is gated off — name the config switch (already
        # present) AND the probe tool so the agent can confirm readiness.
        return ToolError(
            "live mode is disabled (set INKSCAPE_MCP_LIVE_ENABLED=1 to enable); "
            "call check_live_support to inspect live readiness"
        )
    if isinstance(exc, LiveNotAvailable):
        # CAPABILITY-ABSENT: no live session — name the connect + probe tools so the
        # agent establishes one rather than retrying blindly.
        return ToolError(
            "no live session available; call live_connect to connect "
            "(or check_live_support to probe what this host supports)"
        )
    if isinstance(exc, LiveCapabilityUnsupported):
        # CAPABILITY-ABSENT: the active transport lacks this op — name the probe tool so
        # the agent can pick a transport that supports it.
        return ToolError(
            "the active live transport does not support this operation; "
            "call check_live_support to see which transport supports it"
        )
    if isinstance(exc, LiveConnectionError):
        return ToolError("live session communication failed")
    if isinstance(exc, LiveError):
        return ToolError("live operation failed")
    return ToolError("live operation failed")


@mcp.tool
def check_live_support() -> LiveSupport:
    """Report which live transports are available on this host (read-only; no connection).

    When to use: checking live readiness before `live_connect`. To install the socket helper use
    `live_install_helper`; for the full runtime matrix use `list_capabilities`.

    Key params: none. Probes the extension-socket bridge (any OS) and the DBus fast-path
    (Linux/BSD) independently — never assuming one by OS. Safe regardless of whether live mode is
    enabled or a session is running.

    Return shape: `LiveSupport` — `live_enabled`, `any_available`, `best_transport`,
    `helper_installed`, per-transport `transports` probes (best-first), and `notes`.

    Example: `check_live_support()`

    Risk class: low (read-only probe).
    """
    s = get_settings()
    probes = probe_transports(s)
    best = best_available(s, READ_REQUIRED)
    data_dirs: list[str] = []
    with contextlib.suppress(Exception):  # probe best-effort; absence just means "unknown"
        from inkscape_mcp.tools.system import get_cached_capabilities

        caps = get_cached_capabilities()
        data_dirs = [d for d in (caps.system_data_dir, caps.user_data_dir) if d]
    notes: list[str] = []
    if not s.live_enabled:
        notes.append("live mode is disabled by configuration (INKSCAPE_MCP_LIVE_ENABLED opt-out)")
    if not any(p.available for p in probes):
        notes.append("no running Inkscape detected via any transport")
    log_tool_call(_logger, tool="check_live_support")
    return LiveSupport(
        platform=platform.system().lower(),
        live_enabled=s.live_enabled,
        any_available=any(p.available for p in probes),
        best_transport=best.name if best is not None else None,
        helper_installed=is_helper_installed(data_dirs),
        transports=probes,
        notes=notes,
    )


@mcp.tool
def live_connect(prefer: str = "read") -> LiveSession:
    """Connect to a running Inkscape over the best-ranked available transport (enables live).

    When to use: starting a live session before any other `live_*` tool. To probe first use
    `check_live_support`; to tear down use `live_disconnect`.

    Key params: `prefer` selects the profile. `read` (default) is the best READ-capable transport
    (extension-socket primary; full selection/inspect surface) but is MODAL on the socket bridge —
    the GUI freezes for the session. `no_freeze` drives the GUI WITHOUT freezing (Linux DBus
    path): the export-based active-doc read, `live_render_view`, `live_set_viewport`, and
    `live_apply_to_selection` are no-freeze; selection-id reads (`live_get_selection` /
    `live_inspect_selection`) and `live_insert_svg` / `live_set_selected_text` are NOT available
    over DBus and stay modal. Requires the master gate (`INKSCAPE_MCP_LIVE_ENABLED`). With no
    transport available it fails cleanly without affecting headless tools.

    Return shape: `LiveSession` — the chosen `transport`, active document, and connection state.

    Example: `live_connect(prefer="no_freeze")`

    Risk class: medium (establishes a transport; read-only thereafter).
    """
    if prefer not in ("read", "no_freeze"):
        raise ToolError("prefer must be 'read' or 'no_freeze'")
    try:
        session = get_session_manager().connect(prefer=prefer)
    except LiveError as exc:
        _logger.error("live_connect failed", extra={"detail": str(exc)})
        raise _map_live_error(exc) from exc
    log_tool_call(_logger, tool="live_connect", transport=session.transport, prefer=prefer)
    return session


@mcp.tool
def live_disconnect() -> LiveSession:
    """Disconnect the current live session (the X1 disable switch). Idempotent.

    When to use: ending a live session (or as a hard kill switch). To start one use `live_connect`;
    to check state use `live_status`.

    Key params: none. Idempotent — safe to call with no session.

    Return shape: `LiveSession` — the now-disconnected session state.

    Example: `live_disconnect()`

    Risk class: low (tears down the transport; no document mutation).
    """
    session = get_session_manager().disconnect()
    log_tool_call(_logger, tool="live_disconnect")
    return session


@mcp.tool
def live_status() -> LiveSession:
    """Report live-session state: enabled, connected, active transport, available transports.

    When to use: checking whether a session is live before issuing live tools. For per-host
    transport detail use `check_live_support`.

    Key params: none. Never raises — reports "not connected" / "none available" cleanly.

    Return shape: `LiveSession` — `enabled`, `connected`, active transport, available transports.

    Example: `live_status()`

    Risk class: low (read-only).
    """
    return get_session_manager().status()


@mcp.tool
def live_install_helper() -> HelperInstallResult:
    """Install the shipped extension-socket helper into the Inkscape user extensions dir.

    When to use: one-time setup so a running Inkscape can expose the socket bridge. After install,
    probe with `check_live_support` then `live_connect`.

    Key params: none. Copies the fixed-purpose helper (`inkscape_mcp_live.py` + `.inx`). Requires
    the master gate (live is opt-in). Touches no workspace document.

    Return shape: `HelperInstallResult` — `installed_files` and `extensions_dir` (presented
    `~`-relative, never an absolute host path/L5).

    Example: `live_install_helper()`

    Risk class: restricted (writes a server-bundled file under the Inkscape extensions dir).
    """
    s = get_settings()
    if not s.live_enabled:
        # CAPABILITY-ABSENT: name the config switch AND the probe tool.
        raise ToolError(
            "live mode is disabled (set INKSCAPE_MCP_LIVE_ENABLED=1 to enable); "
            "call check_live_support to inspect live readiness"
        )
    try:
        from inkscape_mcp.tools.system import get_cached_capabilities

        caps = get_cached_capabilities()
    except Exception as exc:  # pragma: no cover - probe best-effort
        raise ToolError(_no_extensions_dir_message()) from exc
    if not caps.user_data_dir:
        raise ToolError(_no_extensions_dir_message())

    from inkscape_mcp.live.socket_backend import install_helper

    extensions_dir = Path(caps.user_data_dir) / "extensions"
    try:
        installed = install_helper(extensions_dir)
    except OSError as exc:
        _logger.error("live_install_helper failed", extra={"detail": str(exc)})
        raise ToolError("could not install the live helper extension") from exc
    log_tool_call(_logger, tool="live_install_helper")
    # Return a `~`-relative (or otherwise non-absolute) form rather than an absolute host path so
    # the result is friendlier and never leaks a full home path (L5).
    return HelperInstallResult(
        installed_files=installed, extensions_dir=_present_extensions_dir(extensions_dir)
    )


@mcp.tool
def live_arm_socket() -> SocketArmResult:
    """Auto-arm the extension-socket helper so a programmatic launch gets the FULL live surface.

    When to use: bringing up the FULL perceive/compose live command set without a human
    Extensions-menu click — a programmatic launch otherwise yields only DBus's reduced
    action set. After this returns `armed`, call `live_connect` (the socket bridge is then the best
    transport). To install the helper files first use `live_install_helper`; to probe readiness use
    `check_live_support`.

    Key params: none. Installs the helper if absent, then LAUNCHES a headful Inkscape with the
    helper effect auto-invoked so it binds its loopback socket and advertises a rendezvous — no menu
    click. The socket bridge is the cross-platform primary (NOT bound to one OS). Requires the
    master live gate. GUI-ONLY: the headful launch needs a display; on a HEADLESS host (CI / box,
    no `DISPLAY`/`WAYLAND_DISPLAY`) it fails with a clear, stable message (this leg is documented as
    deferred there) rather than spawning a doomed process. An already-armed session is reused.

    Return shape: `SocketArmResult` — `armed`, `launched` (whether THIS call started Inkscape),
    `helper_installed`, `transport`, and `notes`. No host path is carried (sec.12).

    Example: `live_arm_socket()` then `live_connect()`

    Risk class: restricted (launches a headful Inkscape process and writes a server-bundled helper).
    """
    s = get_settings()
    if not s.live_enabled:
        raise ToolError(
            "live mode is disabled (set INKSCAPE_MCP_LIVE_ENABLED=1 to enable); "
            "call check_live_support to inspect live readiness"
        )

    # Ensure the helper files exist first (idempotent install), so the launched Inkscape can find
    # and run the effect. Reuse the same install path as `live_install_helper`.
    from inkscape_mcp.live.socket_backend import install_helper

    try:
        from inkscape_mcp.tools.system import get_cached_capabilities

        caps = get_cached_capabilities()
    except Exception as exc:  # pragma: no cover - probe best-effort
        raise ToolError(_no_extensions_dir_message()) from exc
    if not caps.user_data_dir:
        raise ToolError(_no_extensions_dir_message())

    data_dirs = [d for d in (caps.system_data_dir, caps.user_data_dir) if d]
    helper_installed = is_helper_installed(data_dirs)
    if not helper_installed:
        try:
            install_helper(Path(caps.user_data_dir) / "extensions")
            helper_installed = True
        except OSError as exc:
            _logger.error("live_arm_socket install failed", extra={"detail": str(exc)})
            raise ToolError("could not install the live helper extension") from exc

    # Was a session already advertising a socket? Then no new launch is needed.
    from inkscape_mcp.live.socket_backend import discover_rendezvous

    already = discover_rendezvous(s) is not None
    try:
        arm_socket_helper(s)
    except SocketArmError as exc:
        _logger.error("live_arm_socket failed", extra={"detail": str(exc)})
        raise ToolError(str(exc)) from exc

    log_tool_call(_logger, tool="live_arm_socket", launched=not already)
    return SocketArmResult(
        armed=True,
        launched=not already,
        helper_installed=helper_installed,
        notes=(
            ["reused an already-armed live session"]
            if already
            else ["launched a headful Inkscape and armed the socket helper"]
        ),
    )


@mcp.tool
def live_get_active_document() -> LiveDocumentRef:
    """Identify the document open in the connected live instance (read-only).

    When to use: confirming WHICH document the live GUI has open. For its full scene use
    `live_get_scene`; for the current selection use `live_get_selection`.

    Key params: none. Requires an established session (`live_connect`).

    Return shape: `LiveDocumentRef` — the active live document's identity.

    Example: `live_get_active_document()`

    Risk class: low (read-only over an established live session).
    """
    try:
        transport = get_session_manager().require_transport()
        result = transport.get_active_document()
    except LiveError as exc:
        raise _map_live_error(exc) from exc
    log_tool_call(_logger, tool="live_get_active_document")
    return result


@mcp.tool
def live_get_selection() -> LiveSelection:
    """Read the current selection in the live instance as object ids (read-only).

    When to use: getting the ids the user selected in the GUI. For their semantic detail use
    `live_inspect_selection`; for the whole scene use `live_get_scene`. Not available over DBus
    (`no_freeze`) — stays on the modal socket transport.

    Key params: none. Requires an established session (`live_connect`).

    Return shape: `LiveSelection` — `count` plus the selected object ids.

    Example: `live_get_selection()`

    Risk class: low (read-only).
    """
    try:
        transport = get_session_manager().require_transport()
        result = transport.get_selection()
    except LiveError as exc:
        raise _map_live_error(exc) from exc
    log_tool_call(_logger, tool="live_get_selection", count=result.count)
    return result


@mcp.tool
def live_inspect_selection() -> LiveSelectionInspection:
    """Inspect the selected objects in the live instance (semantic, by id; read-only).

    When to use: getting structured detail (not just ids) of the GUI selection. For ids only use
    `live_get_selection`; for the whole scene use `live_get_scene`. Not available over DBus
    (`no_freeze`) — stays on the modal socket transport.

    Key params: none. Requires an established session (`live_connect`).

    Return shape: `LiveSelectionInspection` — `count` plus per-object inspection (the headless
    object-inspection shape).

    Example: `live_inspect_selection()`

    Risk class: low (read-only).
    """
    try:
        transport = get_session_manager().require_transport()
        result = transport.inspect_selection()
    except LiveError as exc:
        raise _map_live_error(exc) from exc
    log_tool_call(_logger, tool="live_inspect_selection", count=result.count)
    return result


@mcp.tool
def live_render_view(
    region_x: float | None = None,
    region_y: float | None = None,
    region_width: float | None = None,
    region_height: float | None = None,
    scale: float | None = None,
    fast: bool = False,
) -> LiveRenderResult:
    """Rasterize the live canvas to a PNG in the live artifacts dir (visual feedback).

    When to use: a pixels-only view of the live canvas. For pixels PLUS structured scene use
    `live_get_scene`; for just the selection use `live_export_selection`.

    Key params: with no region the whole canvas renders. Supply ALL four of
    `region_x`/`region_y`/`region_width`/`region_height` (user units; w/h > 0) for a targeted bbox,
    and optional `scale` (>0) to up/downscale. `fast=True` gives a cheap downscaled loop-preview; an
    explicit `scale` always wins. Every numeric is finite-checked and bounded server-side before it
    crosses the transport; the frame comes from the transport renderer, never an OS screenshot
    (deterministic, cross-platform — ADR-006). Served from a per-session cache keyed on
    `(doc_revision, viewport, scale)` so a stale frame is never returned after a change.

    Return shape: `LiveRenderResult` — a workspace-relative PNG path plus render metadata.

    Example: `live_render_view(fast=True)`

    Risk class: low (render to artifact dir; view-only, no Operation Record).
    """
    try:
        region, checked_scale = _validate_frame_params(
            region_x, region_y, region_width, region_height, scale, fast
        )
        result = render_live_view(region=region, scale=checked_scale)  # type: ignore[arg-type]
    except EditError as exc:
        raise _map_live_error(exc) from exc
    except LiveError as exc:
        raise _map_live_error(exc) from exc
    log_tool_call(_logger, tool="live_render_view", region=region is not None, scale=checked_scale)
    return result


@mcp.tool
def live_get_scene(
    region_x: float | None = None,
    region_y: float | None = None,
    region_width: float | None = None,
    region_height: float | None = None,
    scale: float | None = None,
    fast: bool = False,
) -> LiveSceneFrame:
    """Capture one live frame as a PNG PLUS a structured, machine-readable `LiveScene`.

    When to use: the core perception step — the agent reasons over STRUCTURE, not pixels. For
    pixels-only use `live_render_view`; for one loop iteration use `live_session_step`.

    Key params: region/scale/fast work exactly as `live_render_view` (all four region parts at once,
    user units, w/h > 0; optional `scale` > 0; `fast=True` for the downscaled loop preview, explicit
    `scale` wins). Frame rendered through the transport, never an OS screenshot
    (deterministic, cross-platform — ADR-006); served from the per-session cache keyed on
    `(doc_revision, viewport, scale)`. Scene pulled over the fixed `get_scene` command — no
    code or raw Action path (ADR-003). Requires an established session. READ-ONLY (no Operation
    Record, no approval).

    Return shape: `LiveSceneFrame` — `render` (the PNG) plus `scene`: a `LiveScene` carrying the
    active-document identity, selection (ids + bboxes), viewport (zoom/center/visible region), the
    canvas size, and a visible-object summary.

    Example: `live_get_scene(fast=True)`

    Risk class: low (read-only perception; no document mutation, no Operation Record).
    """
    try:
        region, checked_scale = _validate_frame_params(
            region_x, region_y, region_width, region_height, scale, fast
        )
        render = render_live_view(region=region, scale=checked_scale)  # type: ignore[arg-type]
        scene = get_live_scene()
    except EditError as exc:
        raise _map_live_error(exc) from exc
    except LiveError as exc:
        raise _map_live_error(exc) from exc
    log_tool_call(
        _logger,
        tool="live_get_scene",
        region=region is not None,
        selection=scene.selection_count,
        objects=scene.object_count,
    )
    return LiveSceneFrame(render=render, scene=scene)


@mcp.tool
def live_wait_for_change(
    timeout_s: Annotated[float, Field(ge=0.0, le=60.0)] = DEFAULT_WAIT_TIMEOUT_S,
    poll_interval_s: Annotated[float, Field(gt=0.0, le=60.0)] = DEFAULT_POLL_INTERVAL_S,
) -> LiveChange:
    """Block until the live state changes, or the bounded timeout elapses (read-only).

    When to use: between `live_session_step` iterations so the loop reacts to the user's own GUI
    edits instead of busy-rendering. To then re-perceive use `live_get_scene`.

    Key params: `timeout_s` is clamped to at most 60s and `poll_interval_s` is floored, so the wait
    never spins tightly nor blocks forever (it sleeps between cheap polls). Each poll pulls a CHEAP
    state token (small revision marker + selection ids + coarse viewport — never the full doc or a
    PNG; protocol v5), hashed + diffed against the last token. Requires a session; no
    code/raw-Action path (ADR-003). NOTE: the socket helper runs on a snapshot, so within one call
    it cannot observe later GUI edits; the token mechanism is transport-agnostic and detects user
    edits on any transport that recomputes per poll.

    Return shape: `LiveChange` — `changed`, `timed_out`, and the delta flags `selection_changed` /
    `document_changed` / `viewport_changed` (more than one may fire).

    Example: `live_wait_for_change(timeout_s=10)`

    Risk class: low (read-only polling; no document mutation, no Operation Record).
    """
    try:
        result = wait_for_change(timeout_s=timeout_s, poll_interval_s=poll_interval_s)
    except LiveError as exc:
        raise _map_live_error(exc) from exc
    log_tool_call(
        _logger,
        tool="live_wait_for_change",
        changed=result.changed,
        timed_out=result.timed_out,
        selection=result.selection_changed,
        document=result.document_changed,
        viewport=result.viewport_changed,
    )
    return result


@mcp.tool
def live_set_viewport(
    mode: str,
    zoom: float | None = None,
    center_x: float | None = None,
    center_y: float | None = None,
    dx: float | None = None,
    dy: float | None = None,
) -> LiveViewportResult:
    """Control the live canvas viewport: zoom / pan / fit-to-selection / fit-to-page.

    When to use: framing the canvas before a render/capture. To then render use `live_render_view`;
    to edit (not just view) use `live_apply_to_selection`.

    Key params: `mode` is one of the fixed verbs `zoom` | `pan` | `fit_selection` | `fit_page` (no
    raw Action or code path — ADR-003). `zoom` takes a positive `zoom` and optional
    `center_x`/`center_y` to recentre; `pan` takes both `dx` and `dy` (a delta in user units);
    `fit_selection`/`fit_page` take no numerics. Every numeric is finite-checked and bounded
    server-side before it crosses the transport (sec.12). Requires a session. VIEW-ONLY (no
    Operation Record, no approval).

    Return shape: `LiveViewportResult` — the applied viewport state.

    Example: `live_set_viewport("zoom", zoom=2.0)`

    Risk class: low (view-only; no document mutation, no Operation Record).
    """
    try:
        kwargs = validate_viewport(
            mode=mode, zoom=zoom, center_x=center_x, center_y=center_y, dx=dx, dy=dy
        )
        result = set_live_viewport(kwargs)
    except EditError as exc:
        raise _map_live_error(exc) from exc
    except LiveError as exc:
        raise _map_live_error(exc) from exc
    return result


@mcp.tool
def live_sync_to_workspace(dest_path: str) -> LiveSyncResult:
    """Save the live document's current state into the workspace as a NEW tracked document.

    When to use: capturing live work into the headless workspace so the typed tools can act on it.
    For pixels-only feedback use `live_render_view`; to save a HEADLESS doc to disk use
    `save_document_as`.

    Key params: `dest_path` is the new workspace file; RELATIVE anchors to the first workspace root
    (NOT the server CWD) and a not-yet-existing SUBFOLDER is created in-sandbox first (matching
    `save_document_as`; a `..`-escaping / out-of-sandbox dest creates nothing and is rejected with
    `path rejected: outside workspace`). Reads the live SVG and writes it through the policy layer
    (sandbox + symlink guard), registers it (working copy), and records an Operation Record +
    snapshot (ADR-004). An existing destination is REFUSED — sync never overwrites, so a live fault
    cannot damage a workspace file. Requires an established session.

    Return shape: `LiveSyncResult` — the new `doc_id`, `operation_id`, and snapshot links.

    Example: `live_sync_to_workspace("from-live.svg")`

    Risk class: medium (writes a new workspace document, reversible + recorded).
    """
    try:
        result = sync_live_to_workspace(dest_path)
    except LiveSyncError as exc:
        _logger.error("live_sync_to_workspace failed", extra={"detail": str(exc)})
        raise ToolError(str(exc)) from exc
    except LiveError as exc:
        raise _map_live_error(exc) from exc
    log_tool_call(
        _logger,
        tool="live_sync_to_workspace",
        doc_id=result.doc_id,
        operation_id=result.operation_id,
    )
    return result


# --- live WRITE surface (semantic-only, approval-gated) -------------------


@mcp.tool
def live_apply_to_selection(
    approval_token: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
    opacity: float | None = None,
    dx: float | None = None,
    dy: float | None = None,
    scale: float | None = None,
    rotate: float | None = None,
) -> LiveEditResult:
    """Apply a validated style and/or simple transform to the current live selection.

    When to use: editing the GUI selection's style/transform live. To insert markup use
    `live_insert_svg`; to edit text use `live_set_selected_text`; for headless edits use `set_fill`
    / `move_object` / etc.

    Key params: reuses the headless safe-edit semantics — `fill`/`stroke` colour-validated,
    `stroke_width` a CSS length, `opacity` in [0, 1], transform composed from `dx`/`dy` (both
    required together), `scale` (positive), `rotate` (degrees); at least one input required.
    Semantic-only — no arbitrary code, no raw Action (ADR-003). Mutating a running user session is
    HIGH risk: REQUIRES an explicit `approval_token` (refused without one).

    Return shape: `LiveEditResult` — a Live Operation Record with before/after canvas renders,
    syncable to a snapshot via `live_sync_to_workspace`.

    Example: `live_apply_to_selection(approval_token="ok", fill="#3366cc")`

    Risk class: high (approval-gated).
    """
    try:
        style = build_style(fill=fill, stroke=stroke, stroke_width=stroke_width, opacity=opacity)
        transform = build_transform(dx=dx, dy=dy, scale=scale, rotate=rotate)
        if not style and transform is None:
            raise EditError("supply at least one style or transform parameter")

        def op(transport: LiveTransport) -> LiveMutationResult:
            return transport.apply_to_selection(style=style, transform=transform)

        result = run_live_mutation(
            tool="live_apply_to_selection",
            params={"style": style, "transform": transform},
            required_command=LiveCommand.APPLY_TO_SELECTION,
            op=op,
            approval_token=approval_token,
        )
    except (PolicyViolation, EditError, LiveError) as exc:
        _logger.error("live_apply_to_selection failed", extra={"detail": str(exc)})
        raise _map_live_error(exc) from exc
    return result


@mcp.tool
def live_insert_svg(svg_fragment: str, approval_token: str | None = None) -> LiveEditResult:
    """Insert an SVG fragment into the running document.

    When to use: grafting composed markup into the live document. To style the selection use
    `live_apply_to_selection`; for the headless equivalent use `insert_svg_fragment`.

    Key params: `svg_fragment` is parsed through the normative safe parser (no entities, no external
    DTD, no network) and size-bounded before it crosses the transport — only well-formed, safe
    markup is inserted; no code path (ADR-003). Inserting into a running user session is HIGH risk:
    REQUIRES an explicit `approval_token` (refused without one).

    Return shape: `LiveEditResult` — a Live Operation Record with before/after canvas renders,
    syncable to a snapshot.

    Example: `live_insert_svg("<rect .../>", approval_token="ok")`

    Risk class: high (approval-gated).
    """
    try:
        fragment = validate_svg_fragment(svg_fragment)

        def op(transport: LiveTransport) -> LiveMutationResult:
            return transport.insert_svg(fragment)

        result = run_live_mutation(
            tool="live_insert_svg",
            params={"fragment_bytes": len(fragment.encode("utf-8"))},
            required_command=LiveCommand.INSERT_SVG,
            op=op,
            approval_token=approval_token,
        )
    except (PolicyViolation, EditError, LiveError) as exc:
        _logger.error("live_insert_svg failed", extra={"detail": str(exc)})
        raise _map_live_error(exc) from exc
    return result


@mcp.tool
def live_set_selected_text(text: str, approval_token: str | None = None) -> LiveEditResult:
    """Replace the selected text object's content in the running document.

    When to use: changing the selected text object's words live. To restyle the selection use
    `live_apply_to_selection`; for the headless equivalent use `replace_text`.

    Key params: `text` is length-bounded and control-character-rejected (the same guard as the
    headless `replace_text`); it is stored as a text node, so no markup injection is possible.
    Editing a running user session is HIGH risk: REQUIRES an explicit `approval_token` (refused
    without one).

    Return shape: `LiveEditResult` — a Live Operation Record with before/after canvas renders,
    syncable to a snapshot.

    Example: `live_set_selected_text("Hello", approval_token="ok")`

    Risk class: high (approval-gated).
    """
    try:
        safe_text = validate_text(text)

        def op(transport: LiveTransport) -> LiveMutationResult:
            return transport.set_selected_text(safe_text)

        result = run_live_mutation(
            tool="live_set_selected_text",
            params={"text_len": len(safe_text)},
            required_command=LiveCommand.SET_SELECTED_TEXT,
            op=op,
            approval_token=approval_token,
        )
    except (PolicyViolation, EditError, LiveError) as exc:
        _logger.error("live_set_selected_text failed", extra={"detail": str(exc)})
        raise _map_live_error(exc) from exc
    return result


@mcp.tool
def live_export_selection() -> LiveExportResult:
    """Export just the current live selection to a PNG under the live artifacts dir.

    When to use: a PNG of only the GUI selection. For the whole canvas use `live_render_view`; for
    pixels plus structure use `live_get_scene`.

    Key params: none. Read-only feedback (no mutation, no approval, no Operation Record), mirroring
    `live_render_view`. Requires an established session.

    Return shape: `LiveExportResult` — a workspace-relative PNG path under the live artifacts dir.

    Example: `live_export_selection()`

    Risk class: low (render to artifact dir).
    """
    try:
        result = export_live_selection()
    except LiveError as exc:
        raise _map_live_error(exc) from exc
    return result


@mcp.tool
def live_diff_view(operation_id: str) -> LiveDiffResult:
    """Produce a FOCUSED, annotated before/after visual diff of a live operation.

    When to use: visualizing what one live mutation changed. To produce a mutation to diff use
    `live_apply_to_selection` / `live_insert_svg` / `live_set_selected_text` (or
    `live_session_step`, which calls this internally).

    Key params: `operation_id` names the Live Operation Record. The tool REUSES the before/after
    frames the mutation already captured (`run_live_mutation` persists `preview_before` /
    `preview_after`), pixel-diffs them to a CHANGED-REGION bbox, and emits ONE annotated overlay
    highlighting it plus the current selection outline (best-effort when a session is connected).
    Frames are resolved VIA the `operation_id` (never a raw client path) and sandbox-validated under
    the live artifacts dir before any bytes are read. Identical-dimension frames required; a size
    mismatch is a stable error. ARTIFACT-ONLY — no mutation, no Operation Record routing, no
    approval, no network.

    Return shape: `LiveDiffResult` — a workspace-relative overlay PNG path, the `operation_id`, the
    pixel-space `changed_bbox` (null when the frames are identical), and `highlighted_ids`; the diff
    path is linked back onto the record's `diff_artifacts`.

    Example: `live_diff_view(operation_id)`

    Risk class: low (artifact-only; reads + annotates two existing frames, no mutation, no record).
    """
    # Best-effort: pull the current scene's selection bboxes + canvas to annotate the overlay.
    # A connected session is NOT required — without one we still emit the changed-region highlight.
    selection: list[SceneSelectionItem] | None = None
    canvas: BBox | None = None
    with contextlib.suppress(Exception):
        scene = get_live_scene()
        selection = scene.selection
        if scene.canvas.width is not None and scene.canvas.height is not None:
            canvas = BBox(x=0.0, y=0.0, width=scene.canvas.width, height=scene.canvas.height)
    try:
        result = diff_live_operation(operation_id, selection=selection, canvas=canvas)
    except LiveDiffError as exc:
        _logger.error("live_diff_view failed", extra={"detail": str(exc)})
        raise ToolError(str(exc)) from exc
    except LiveError as exc:
        raise _map_live_error(exc) from exc
    log_tool_call(
        _logger,
        tool="live_diff_view",
        operation_id=result.operation_id,
        changed=result.changed_bbox is not None,
        highlighted=len(result.highlighted_ids),
    )
    return result


@mcp.tool
def live_session_step(
    action: StepAction | None = None,
    approval_token: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
    opacity: float | None = None,
    dx: float | None = None,
    dy: float | None = None,
    scale: float | None = None,
    rotate: float | None = None,
    svg_fragment: str | None = None,
    text: str | None = None,
) -> LiveSessionStepResult:
    """Run ONE perceive→decide→act→observe iteration of the live-view loop.

    When to use: the flagship loop step — call it repeatedly to drive a live edit loop (use
    `live_wait_for_change` between steps to react to the user's edits). It COMPOSES the existing
    live tools (ADR-006); for a single standalone edit call `live_apply_to_selection` /
    `live_insert_svg` / `live_set_selected_text` directly. Each call is one bounded iteration — no
    server-side autonomous run.

    Key params: `action` is the AGENT's decision (this tool embeds no LLM), one of the FIXED set
    `apply` | `insert_svg` | `set_text` (no raw-Action/code path — ADR-002/003; an out-of-enum
    action is rejected). OMIT `action` for a PERCEIVE-ONLY step (mutates nothing, no Operation
    Record). When acting: `apply` takes `fill`/`stroke`/`stroke_width`/`opacity` and/or
    `dx`/`dy`/`scale`/`rotate`; `insert_svg` takes a safe-parsed `svg_fragment`; `set_text` takes a
    control-char-checked `text`. The act runs through `run_live_mutation` (the SAME path as the
    standalone tools); mutating a running session is HIGH risk and REQUIRES an explicit
    `approval_token`. Requires a session.

    Return shape: `LiveSessionStepResult` — always the PERCEIVE scene + frame; after an act also the
    `operation_id`, a focused `live_diff_view` artifact, and the after scene/frame.

    Example: `live_session_step(action="apply", approval_token="ok", fill="#3366cc")`

    Risk class: high when it acts (routes through `run_live_mutation` — HIGH + approval); low when
    perceive-only (read-only; no mutation, no Operation Record).
    """
    try:
        result = run_session_step(
            action=action,
            approval_token=approval_token,
            fill=fill,
            stroke=stroke,
            stroke_width=stroke_width,
            opacity=opacity,
            dx=dx,
            dy=dy,
            scale=scale,
            rotate=rotate,
            svg_fragment=svg_fragment,
            text=text,
        )
    except (PolicyViolation, EditError, LiveError) as exc:
        _logger.error("live_session_step failed", extra={"detail": str(exc)})
        raise _map_live_error(exc) from exc
    except Exception as exc:
        # Orchestrator composes many calls; never let an unexpected exception surface a host-path
        # traceback to the client — fold it into the stable, host-path-free mapping.
        _logger.error("live_session_step failed", extra={"detail": type(exc).__name__})
        raise _map_live_error(exc) from exc
    return result
