"""Extension-socket backend — the primary, cross-platform live transport.

A server-shipped, fixed-purpose `inkex` helper (``helper_extension/inkscape_mcp_live.py``) runs
inside the live Inkscape process, binds a LOOPBACK-ONLY socket, and advertises it by writing a
rendezvous file (port + random token). This module is the server-side *client*: it discovers the
rendezvous, dials ``127.0.0.1:<port>``, performs the token handshake, and exchanges the fixed
``protocol.py`` command schema. No arbitrary code, no raw Action strings (ADR-003).

Security (sec.12, restricted): connections target loopback only; the per-session token (minted by
the helper, never guessable) is required by the handshake; messages are size-capped; a missing
helper / no live session degrades to "not available" — never an exception that could damage
workspace files.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.document.inspect import ObjectInfo
from inkscape_mcp.live.protocol import (
    LOOPBACK_HOST,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    LiveCommand,
    ProtocolError,
    build_request,
    parse_response,
    recv_message,
)
from inkscape_mcp.live.transport import (
    LiveCapabilityUnsupported,
    LiveConnectionError,
    LiveDocumentRef,
    LiveMutationResult,
    LiveScene,
    LiveSelection,
    LiveSelectionInspection,
    LiveStateToken,
    LiveTransport,
    LiveViewportResult,
    RenderRegion,
    TransportProbe,
)
from inkscape_mcp.logging_setup import get_logger

_logger = get_logger("live.socket")

#: Rendezvous filename the helper writes and the client looks for.
RENDEZVOUS_FILENAME = "inkscape-mcp-live.json"

#: Env override pointing directly at the rendezvous file (highest-priority discovery source).
ENV_RENDEZVOUS = "INKSCAPE_MCP_LIVE_RENDEZVOUS"

#: Helper extension module filename (also the install marker the runtime probe looks for).
HELPER_MODULE = "inkscape_mcp_live.py"
HELPER_INX = "inkscape_mcp_live.inx"


class Rendezvous(BaseModel):
    """Parsed contents of the helper's rendezvous file."""

    port: int
    token: str
    protocol_version: int = PROTOCOL_VERSION
    pid: int | None = None


def _helper_dir() -> Path:
    """Directory of the shipped helper extension (package data)."""
    return Path(__file__).parent / "helper_extension"


def helper_source_paths() -> tuple[Path, Path]:
    """Return the shipped (``.py``, ``.inx``) helper paths bundled with the server."""
    base = _helper_dir()
    return base / HELPER_MODULE, base / HELPER_INX


def is_helper_installed(data_dirs: list[str]) -> bool:
    """Whether the helper module is present under any data dir's ``extensions/``."""
    for data_dir in data_dirs:
        marker = Path(data_dir) / "extensions" / HELPER_MODULE
        try:
            if marker.is_file():
                return True
        except OSError:
            continue
    return False


def install_helper(extensions_dir: str | Path) -> list[str]:
    """Copy the shipped helper (``.py`` + ``.inx``) into `extensions_dir`.

    Creates the directory if needed and overwrites an older copy (idempotent upgrade). Returns
    the list of installed filenames. Raises `OSError` on a filesystem failure; the tool layer
    maps that to a stable message.
    """
    target = Path(extensions_dir)
    target.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for src in helper_source_paths():
        dst = target / src.name
        shutil.copyfile(src, dst)
        installed.append(src.name)
    _logger.info("live helper installed", extra={"event": "file_io", "extensions_dir": str(target)})
    return installed


#: GApplication / extension id of the shipped helper effect (must match `inkscape_mcp_live.inx`).
#: This is the action token a programmatic launch invokes via ``--actions`` so the helper arms the
#: socket without a human Extensions-menu click. It is a FIXED server-owned constant,
#: never client input — the only value spliced into the launch argv besides server-controlled paths.
HELPER_ACTION_ID = "org.inkscape_mcp.live.noprefs"


class SocketArmError(Exception):
    """Auto-arming the extension-socket helper failed (no display, no binary, or arm timed out).

    Carries a stable, host-path-free public message; the tool layer maps it to `ToolError`.
    """


def _display_available() -> bool:
    """Best-effort: whether a GUI display is reachable so a HEADFUL Inkscape can actually start.

    The extension-socket helper must run inside a LIVE (headful) Inkscape to serve the socket for
    the full perceive/compose surface; a headless host (no ``DISPLAY`` / ``WAYLAND_DISPLAY`` on
    Linux/BSD, including CI — Tier-B) cannot bring one up. macOS/Windows always have a window
    server, so this returns True there. This gates the GUI-only launch so a headless call fails with
    a clear, documented message rather than spawning a doomed process.
    """
    system = platform.system().lower()
    if system in ("darwin", "windows"):
        return True
    # Linux/BSD: a windowing display must be advertised in the environment.
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _arm_launch_argv(binary: str, doc_path: Path) -> list[str]:
    """Build the headful-launch argv that auto-invokes the helper effect (arg-list, no shell).

    Opens ``doc_path`` (a server-controlled path) in a GUI Inkscape and runs the FIXED helper action
    (:data:`HELPER_ACTION_ID`) so the extension binds its loopback socket and writes the rendezvous
    file with NO human menu click. ``--with-gui`` keeps the window (and thus the serving extension)
    alive. No client value is interpolated — only the server binary, the server doc path, and the
    fixed action token (sec.12: arg-list only, never a shell string).
    """
    return [
        binary,
        "--with-gui",
        f"--actions={HELPER_ACTION_ID}",
        str(doc_path),
    ]


def arm_socket_helper(settings: Settings | None = None) -> Rendezvous:
    """Launch a headful Inkscape that auto-arms the socket helper, then await its rendezvous.

    The full perceive/compose live surface needs the extension-socket bridge running INSIDE a live
    Inkscape; a programmatic DBus launch only yields the reduced action set, and arming the socket
    otherwise required a human Extensions-menu click. This launches a GUI Inkscape with the helper
    effect auto-invoked (:func:`_arm_launch_argv`) so the socket binds and the rendezvous lands with
    no menu click.

    Cross-platform by construction (the socket bridge is the primary transport on every OS — never
    bound to one OS). On a HEADLESS host (no display — CI / a remote box) the GUI cannot start, so
    this raises :class:`SocketArmError` with a clear message rather than spawning a doomed process
    (the GUI-only leg is documented as deferred there, mirroring the Tier-B skip). Returns
    the discovered :class:`Rendezvous` once the helper advertises its socket within the configured
    timeout. Security (sec.12): arg-list launch only; the launched doc is a server-minted temp file;
    no client value reaches the argv.
    """
    s = settings if settings is not None else get_settings()

    # Already armed? Reuse an advertising session rather than launching a second window. This is
    # checked FIRST, before the binary/display gates: reusing an existing socket needs neither an
    # inkscape binary nor a GUI display, so a headless host (CI) can still reuse a session armed
    # elsewhere.
    existing = discover_rendezvous(s)
    if existing is not None:
        return existing

    binary = shutil.which("inkscape")
    if binary is None:
        raise SocketArmError("inkscape binary not found; cannot arm the live socket helper")
    if not _display_available():
        raise SocketArmError(
            "no GUI display available to launch a live Inkscape (the socket helper needs a "
            "headful instance); arm it from a desktop session, or run live_install_helper and "
            "invoke the helper from Inkscape's Extensions menu"
        )

    # Server-minted temp doc to open (never client input). A blank valid SVG is enough for the
    # helper to bind; it is left on disk for the GUI session and cleaned up by the OS temp policy.
    fd, tmp_name = tempfile.mkstemp(prefix="inkscape-mcp-arm-", suffix=".svg")
    doc_path = Path(tmp_name)
    with os.fdopen(fd, "wb") as fh:
        fh.write(
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" '
            b'viewBox="0 0 100 100"></svg>\n'
        )

    argv = _arm_launch_argv(binary, doc_path)
    try:
        # Detached background GUI: it must OUTLIVE this call to keep serving the socket, so it is
        # NOT run through the capturing/timeout `run_process` path. shell=False, fixed argv.
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise SocketArmError("could not launch a live Inkscape to arm the socket helper") from exc

    # Poll for the rendezvous the helper writes once its socket is bound (bounded by the per-process
    # timeout so a never-arming launch fails cleanly instead of hanging).
    deadline = time.monotonic() + max(5.0, s.process_timeout_s)
    while time.monotonic() < deadline:
        rv = discover_rendezvous(s)
        if rv is not None:
            return rv
        time.sleep(0.25)
    raise SocketArmError(
        "live Inkscape launched but the socket helper did not advertise a rendezvous in time"
    )


def _rendezvous_candidates(settings: Settings) -> list[Path]:
    """Ordered list of paths to search for the rendezvous file.

    Order: explicit env override → Inkscape user data dir (best-effort, from the capability
    cache) → the OS temp dir. The first parseable, valid file wins.
    """
    candidates: list[Path] = []
    override = os.environ.get(ENV_RENDEZVOUS, "").strip()
    if override:
        candidates.append(Path(override))

    # Inkscape user data dir, if a probe is cached (avoid importing the tools layer eagerly).
    with contextlib.suppress(Exception):  # discovery is best-effort; never block on the probe
        from inkscape_mcp.tools.system import get_cached_capabilities

        caps = get_cached_capabilities()
        if caps.user_data_dir:
            candidates.append(Path(caps.user_data_dir) / RENDEZVOUS_FILENAME)

    candidates.append(Path(tempfile.gettempdir()) / RENDEZVOUS_FILENAME)
    return candidates


def discover_rendezvous(settings: Settings) -> Rendezvous | None:
    """Find and parse the helper's rendezvous file, or None if no live session advertises one.

    A malformed or out-of-version file is treated as "not present" (returns None) rather than an
    error — discovery must always degrade cleanly.
    """
    for path in _rendezvous_candidates(settings):
        try:
            if not path.is_file():
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _logger.debug("skip unreadable rendezvous candidate")
            continue
        if not isinstance(raw, dict):
            continue
        try:
            rv = Rendezvous.model_validate(raw)
        except ValidationError:
            _logger.debug("skip malformed rendezvous candidate")
            continue
        if rv.protocol_version != PROTOCOL_VERSION:
            _logger.warning(
                "live rendezvous protocol mismatch",
                extra={"found": rv.protocol_version, "expected": PROTOCOL_VERSION},
            )
            continue
        if not (1 <= rv.port <= 65535) or not rv.token:
            continue
        return rv
    return None


class ExtensionSocketTransport(LiveTransport):
    """Loopback socket client speaking the fixed `protocol.py` schema to the helper extension."""

    name: ClassVar[str] = "extension-socket"
    #: Primary transport: ranked above DBus because it serves the full read AND write surface.
    rank: ClassVar[int] = 20
    supported_commands: ClassVar[frozenset[LiveCommand]] = frozenset(
        {
            LiveCommand.PING,
            LiveCommand.GET_ACTIVE_DOCUMENT,
            LiveCommand.GET_SELECTION,
            LiveCommand.INSPECT_SELECTION,
            LiveCommand.GET_DOCUMENT_SVG,
            LiveCommand.RENDER_VIEW,
            LiveCommand.APPLY_TO_SELECTION,
            LiveCommand.INSERT_SVG,
            LiveCommand.SET_SELECTED_TEXT,
            LiveCommand.EXPORT_SELECTION,
            LiveCommand.SET_VIEWPORT,
            LiveCommand.GET_SCENE,
            LiveCommand.GET_STATE_TOKEN,
        }
    )

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._sock: socket.socket | None = None
        self._token: str | None = None
        self._capabilities: list[str] = []

    @classmethod
    def probe(cls, settings: Settings) -> TransportProbe:
        rv = discover_rendezvous(settings)
        if rv is None:
            data_dirs = _data_dirs_from_cache()
            installed = is_helper_installed(data_dirs)
            detail = (
                "helper installed; no live session advertising a socket"
                if installed
                else "no rendezvous; helper not detected as installed"
            )
            return TransportProbe(
                name=cls.name,
                available=False,
                rank=cls.rank,
                supported_commands=[c.value for c in sorted(cls.supported_commands)],
                detail=detail,
            )
        return TransportProbe(
            name=cls.name,
            available=True,
            rank=cls.rank,
            supported_commands=[c.value for c in sorted(cls.supported_commands)],
            detail=f"live session reachable on {LOOPBACK_HOST}:{rv.port}",
        )

    def connect(self) -> None:
        rv = discover_rendezvous(self._settings)
        if rv is None:
            raise LiveConnectionError("no live session available (helper not running)")
        timeout = self._settings.process_timeout_s
        try:
            sock = socket.create_connection((LOOPBACK_HOST, rv.port), timeout=timeout)
        except OSError as exc:
            raise LiveConnectionError("could not connect to the live session") from exc
        sock.settimeout(timeout)
        self._sock = sock
        self._token = rv.token
        try:
            result = self._request(LiveCommand.HELLO, {"client": "inkscape-mcp"})
        except (ProtocolError, LiveConnectionError) as exc:
            self.disconnect()
            raise LiveConnectionError("live handshake failed") from exc
        if result.get("protocol_version") != PROTOCOL_VERSION:
            self.disconnect()
            raise LiveConnectionError("live helper protocol version mismatch")
        caps = result.get("capabilities")
        self._capabilities = [str(c) for c in caps] if isinstance(caps, list) else []
        _logger.info(
            "live session connected",
            extra={"event": "process_exec", "transport": self.name, "port": rv.port},
        )

    def disconnect(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:  # pragma: no cover - best-effort close
                pass
        self._sock = None
        self._token = None
        self._capabilities = []

    def is_connected(self) -> bool:
        return self._sock is not None

    def _request(
        self, command: LiveCommand, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send one fixed-schema command, return the validated `result` dict.

        Raises `LiveConnectionError` if not connected or the socket fails, `ProtocolError` if the
        peer's frame is out of schema. The token is attached to every request (handshake-on-every).
        """
        if self._sock is None or self._token is None:
            raise LiveConnectionError("not connected to a live session")
        try:
            self._sock.sendall(build_request(command, self._token, params))
            # Keep the protocol constant as the authoritative ceiling; an operator may tighten it
            # below via max_output_bytes but never widen it past the schema's own cap.
            cap = min(MAX_MESSAGE_BYTES, self._settings.max_output_bytes)
            frame = recv_message(self._sock, cap)
        except (OSError, ProtocolError) as exc:
            raise LiveConnectionError("live communication failed") from exc
        return parse_response(frame)

    def get_active_document(self) -> LiveDocumentRef:
        result = self._request(LiveCommand.GET_ACTIVE_DOCUMENT)
        return LiveDocumentRef(
            name=_opt_str(result.get("name")),
            path=_opt_str(result.get("path")),
            object_count=_opt_int(result.get("object_count")),
        )

    def get_selection(self) -> LiveSelection:
        result = self._request(LiveCommand.GET_SELECTION)
        ids = result.get("object_ids")
        object_ids = [str(i) for i in ids] if isinstance(ids, list) else []
        return LiveSelection(object_ids=object_ids, count=len(object_ids))

    def inspect_selection(self) -> LiveSelectionInspection:
        result = self._request(LiveCommand.INSPECT_SELECTION)
        raw_objects = result.get("objects")
        objects: list[ObjectInfo] = []
        if isinstance(raw_objects, list):
            for item in raw_objects:
                if not isinstance(item, dict):
                    continue
                objects.append(
                    ObjectInfo(
                        id=_opt_str(item.get("id")),
                        tag=str(item.get("tag", "")),
                        label=_opt_str(item.get("label")),
                        has_style=bool(item.get("has_style", False)),
                    )
                )
        return LiveSelectionInspection(objects=objects, count=len(objects))

    def get_document_svg(self) -> str:
        result = self._request(LiveCommand.GET_DOCUMENT_SVG)
        svg = result.get("svg")
        if not isinstance(svg, str) or not svg:
            raise LiveConnectionError("live document could not be read")
        return svg

    def render_view(self, region: RenderRegion | None = None, scale: float | None = None) -> bytes:
        params: dict[str, Any] = {}
        if region is not None:
            params["region"] = [region.x, region.y, region.width, region.height]
        if scale is not None:
            params["scale"] = scale
        result = self._request(LiveCommand.RENDER_VIEW, params or None)
        return self._decode_png(result, "live render is not available")

    # --- View-only surface ----------------------------------------------

    def set_viewport(
        self,
        *,
        mode: str,
        zoom: float | None = None,
        center: tuple[float, float] | None = None,
        dx: float | None = None,
        dy: float | None = None,
    ) -> LiveViewportResult:
        params: dict[str, Any] = {"mode": mode}
        if zoom is not None:
            params["zoom"] = zoom
        if center is not None:
            params["center"] = [center[0], center[1]]
        if dx is not None:
            params["dx"] = dx
        if dy is not None:
            params["dy"] = dy
        result = self._request(LiveCommand.SET_VIEWPORT, params)
        applied_mode = result.get("mode")
        return LiveViewportResult(
            mode=str(applied_mode) if isinstance(applied_mode, str) else mode,
            applied=bool(result.get("applied", True)),
            detail=str(result.get("detail")) if isinstance(result.get("detail"), str) else "",
        )

    def get_scene(self) -> LiveScene:
        # Import here to avoid a module-load cycle (scene.py imports session.py, which is fine,
        # but scene_from_result only needs the coercion helpers).
        from inkscape_mcp.live.scene import scene_from_result

        result = self._request(LiveCommand.GET_SCENE)
        # The active-document identity comes from the authoritative read command, not the wire
        # scene payload, so a malformed scene can never spoof a document path.
        active_document = self.get_active_document()
        return scene_from_result(result, active_document)

    def get_state_token(self) -> tuple[LiveStateToken, list[str]]:
        # Hash the cheap wire components server-side (no full doc / PNG crosses for a poll). Import
        # here to avoid a module-load cycle (events.py imports session.py).
        from inkscape_mcp.live.events import token_from_result

        result = self._request(LiveCommand.GET_STATE_TOKEN)
        return token_from_result(result)

    # --- Semantic WRITE surface -----------------------------------------

    def apply_to_selection(
        self, *, style: dict[str, str], transform: str | None
    ) -> LiveMutationResult:
        result = self._request(
            LiveCommand.APPLY_TO_SELECTION, {"style": style, "transform": transform}
        )
        return _mutation_result(result)

    def insert_svg(self, svg_fragment: str) -> LiveMutationResult:
        result = self._request(LiveCommand.INSERT_SVG, {"svg": svg_fragment})
        return _mutation_result(result)

    def set_selected_text(self, text: str) -> LiveMutationResult:
        result = self._request(LiveCommand.SET_SELECTED_TEXT, {"text": text})
        return _mutation_result(result)

    def export_selection(self) -> bytes:
        result = self._request(LiveCommand.EXPORT_SELECTION)
        return self._decode_png(result, "live selection export is not available")

    @staticmethod
    def _decode_png(result: dict[str, Any], unsupported_message: str) -> bytes:
        encoded = result.get("png_base64")
        if not isinstance(encoded, str) or not encoded:
            raise LiveCapabilityUnsupported(unsupported_message)
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise LiveConnectionError("live render payload was malformed") from exc


def _data_dirs_from_cache() -> list[str]:
    try:
        from inkscape_mcp.tools.system import get_cached_capabilities

        caps = get_cached_capabilities()
        return [d for d in (caps.system_data_dir, caps.user_data_dir) if d]
    except Exception:  # pragma: no cover - best-effort
        return []


def _mutation_result(result: dict[str, Any]) -> LiveMutationResult:
    """Build a `LiveMutationResult` from a helper WRITE response (defensive coercion)."""
    raw_ids = result.get("affected_ids")
    affected_ids = [str(i) for i in raw_ids] if isinstance(raw_ids, list) else []
    detail = result.get("detail")
    return LiveMutationResult(
        affected_ids=affected_ids,
        count=len(affected_ids),
        detail=str(detail) if isinstance(detail, str) else "",
        undo_friendly=bool(result.get("undo_friendly", False)),
    )


def _opt_str(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _opt_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) else None
