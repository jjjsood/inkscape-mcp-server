"""Live administration: host support probe, helper install, socket arming, live-error mapping.

The engine behind the live admin tools. Everything here is host/session scaffolding rather than
document editing: `check_live_support` probes every transport, `live_install_helper` copies the
shipped extension-socket helper into the Inkscape user extensions dir, `live_arm_socket` launches a
headful Inkscape with the helper auto-invoked, and `map_live_error` turns a live-layer exception
into a stable, host-path-free `ToolError` (sec.12) that names the next tool to call.
"""

from __future__ import annotations

import contextlib
import platform
from pathlib import Path

from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from inkscape_mcp.config import get_settings
from inkscape_mcp.edit.dom import EditError
from inkscape_mcp.live.selection import READ_REQUIRED, best_available, probe_transports
from inkscape_mcp.live.socket_backend import SocketArmError, arm_socket_helper, is_helper_installed
from inkscape_mcp.live.transport import (
    LiveCapabilityUnsupported,
    LiveConnectionError,
    LiveDisabled,
    LiveError,
    LiveNotAvailable,
    TransportProbe,
)
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.workspace.risk import PolicyViolation

_logger = get_logger("live.admin")


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


def map_live_error(exc: Exception) -> ToolError:
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


def check_live_support() -> LiveSupport:
    """Report which live transports are available on this host (read-only; no connection)."""
    s = get_settings()
    probes = probe_transports(s)
    best = best_available(s, READ_REQUIRED)
    data_dirs: list[str] = []
    with contextlib.suppress(Exception):  # probe best-effort; absence just means "unknown"
        from inkscape_mcp.runtime.capabilities import get_cached_capabilities

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


def live_install_helper() -> HelperInstallResult:
    """Install the shipped extension-socket helper into the Inkscape user extensions dir."""
    s = get_settings()
    if not s.live_enabled:
        # CAPABILITY-ABSENT: name the config switch AND the probe tool.
        raise ToolError(
            "live mode is disabled (set INKSCAPE_MCP_LIVE_ENABLED=1 to enable); "
            "call check_live_support to inspect live readiness"
        )
    try:
        from inkscape_mcp.runtime.capabilities import get_cached_capabilities

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


def live_arm_socket() -> SocketArmResult:
    """Auto-arm the extension-socket helper (installs it first when absent)."""
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
        from inkscape_mcp.runtime.capabilities import get_cached_capabilities

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
