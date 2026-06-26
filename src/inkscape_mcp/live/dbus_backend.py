"""DBus backend — Linux/BSD no-freeze fast-path (E3-03 + E3-07).

Where a session bus is present, Inkscape 1.2+ exports its GTK GAction group over DBus
(``org.gtk.Actions`` on ``org.inkscape.Inkscape``). This backend drives that interface via the
``gdbus`` CLI (arg lists only, ``shell=False`` — sec.12). Unlike the extension-socket bridge (a
modal ``inkex`` effect extension that freezes the GUI for the whole session), an
``org.gtk.Actions.Activate`` call runs the action in Inkscape's own GLib main loop — the same path
as the user clicking a menu item — so it does **NOT** freeze the GUI. This backend therefore marks
itself ``no_freeze = True`` and serves the live operations that map cleanly to GAction activations.

What is faithfully serviceable over the action surface (verified against a live Inkscape 1.4.3,
2026-06-14) — all no-freeze:

* **Structured read via export-to-file (E3-07 workaround).** ``org.gtk.Actions`` cannot *return* the
  document, but it can be told to *export* it: ``export-filename`` → ``export-type`` → ``export-do``
  writes the live document to a temp path the server then reads. This yields ``get_document_svg``
  (plain SVG), ``render_view`` (PNG) and ``get_active_document`` (parsed from the exported SVG) with
  no GUI freeze on Linux. (Honest side effect: it sets the instance's export options — the action
  surface is stateful — but never mutates the document.)
* **Viewport** via the per-window action group (``canvas-zoom-page`` / ``canvas-zoom-selection`` /
  ``canvas-zoom-absolute``) — ``set_viewport`` for ``fit_page`` / ``fit_selection`` / ``zoom``.
* **Style/transform on the current selection** via ``object-set-property`` and
  ``transform-translate`` / ``transform-rotate`` / ``transform-scale`` — ``apply_to_selection``,
  applied as undoable Inkscape steps.

What is NOT serviceable over the action surface (no GAction returns selection ids or accepts
arbitrary markup), so it keeps falling back to the extension-socket backend (with its freeze):
``get_selection`` / ``inspect_selection`` (no action returns ids), ``insert_svg`` /
``set_selected_text`` (no faithful GAction). Those raise ``LiveCapabilityUnsupported`` here.

No raw Action string is ever exposed to callers (ADR-003): every activation is built server-side
from validated parameters, and every value crossing into a GVariant text literal is guarded against
quote/backslash/control-character injection. ``--app-id-tag`` is only a GApplication id tag and is
never used as a control API (project live-mode rule).
"""

from __future__ import annotations

import math
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import ClassVar

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.edit.dom import EditError
from inkscape_mcp.live.protocol import LiveCommand
from inkscape_mcp.live.transport import (
    LiveCapabilityUnsupported,
    LiveConnectionError,
    LiveDocumentRef,
    LiveError,
    LiveMutationResult,
    LiveSelection,
    LiveSelectionInspection,
    LiveTransport,
    LiveViewportResult,
    RenderRegion,
    TransportProbe,
)
from inkscape_mcp.logging_setup import get_logger
from inkscape_mcp.workspace.subprocess_exec import ProcessError, run_process
from inkscape_mcp.workspace.xml_safety import UnsafeXMLError, parse_svg_bytes

_logger = get_logger("live.dbus")

#: Well-known bus name / object path for Inkscape's exported GAction group.
INKSCAPE_BUS_NAME = "org.inkscape.Inkscape"
INKSCAPE_OBJECT_PATH = "/org/inkscape/Inkscape"
GTK_ACTIONS_IFACE = "org.gtk.Actions"

#: SVG namespaces used when reading an exported document.
_SODIPODI_NS = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.0.dtd"

#: Values that may be interpolated into a GVariant text literal must contain none of these
#: (quote / backslash / control characters) — they would break out of the ``'...'`` quoting.
#: Every value reaching the builder is already server-validated; this is defense in depth (X1).
_GVARIANT_UNSAFE_RE = re.compile(r"['\\\x00-\x1f\x7f]")

#: Inkscape's natural raster resolution: 96 dpi == 1 user unit (px). A render `scale` maps to dpi.
_BASE_DPI = 96.0

#: Composed-transform grammar (the exact, server-generated output of `live.edit.build_transform`).
#: Re-parsed here into discrete `transform-*` activations; anything not matching is refused so a
#: hand-crafted transform string can never reach the action surface unchecked.
_NUM = r"-?\d+(?:\.\d+)?"
_TRANSLATE_RE = re.compile(rf"^translate\((?P<dx>{_NUM}),(?P<dy>{_NUM})\)$")
_ROTATE_RE = re.compile(rf"^rotate\((?P<deg>{_NUM})\)$")
_SCALE_RE = re.compile(rf"^scale\((?P<f>{_NUM})\)$")


def _session_bus_present() -> bool:
    """Whether a session bus address is exported (necessary for any DBus call)."""
    return bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS", "").strip())


def _gdbus() -> str | None:
    return shutil.which("gdbus")


# --- GVariant text builders (guarded) ---------------------------------------


def _guard_variant_str(value: str) -> str:
    """Return `value` if safe to embed in a GVariant single-quoted literal, else raise.

    Refuses any quote / backslash / control character (sec.12, X1). Values reaching here are
    already server-validated (server-minted temp paths, finite-checked numbers, charset-checked
    ids), so a failure indicates a bug or tampering, not normal input.
    """
    if _GVARIANT_UNSAFE_RE.search(value):
        raise LiveError("unsafe value rejected before DBus activation")
    return value


def _variant_string(value: str) -> str:
    """A one-element ``av`` parameter carrying a single string variant: ``[<'value'>]``."""
    return f"[<'{_guard_variant_str(value)}'>]"


def _variant_bool(value: bool) -> str:
    """A one-element ``av`` parameter carrying a single boolean variant: ``[<true>]``."""
    return f"[<{'true' if value else 'false'}>]"


def _variant_double(value: float) -> str:
    """A one-element ``av`` parameter carrying a single double variant: ``[<2.0>]``.

    Rejects non-finite values (defense in depth — callers validate upstream): ``nan``/``inf`` would
    format to bare tokens that gdbus silently mishandles rather than the server detecting the fault.
    """
    num = float(value)
    if not math.isfinite(num):
        raise LiveError("non-finite value rejected before DBus activation")
    return f"[<{num!r}>]"


def _variant_empty() -> str:
    """An empty ``av`` parameter (for actions that take no value): ``@av []``."""
    return "@av []"


class DBusTransport(LiveTransport):
    """Drives ``org.gtk.Actions`` on the session bus — the Linux/BSD no-freeze action path."""

    name: ClassVar[str] = "dbus"
    #: Fast-path but capability-limited; ranked below the socket backend so the default read-mode
    #: connect still prefers the full-read socket transport. The no-freeze path is reached via the
    #: ``no_freeze`` connect preference (E3-07), not by out-ranking the socket for reads.
    rank: ClassVar[int] = 10
    #: Runs ops in Inkscape's own GLib main loop ⇒ no modal GUI freeze (E3-07).
    no_freeze: ClassVar[bool] = True
    #: Honest capability set (verified 2026-06-14): liveness, export-based reads, viewport, and
    #: style/transform on the current selection. NOT get/inspect-selection (no action returns ids),
    #: NOT insert-svg / set-selected-text (no faithful GAction) — those stay on the socket backend.
    supported_commands: ClassVar[frozenset[LiveCommand]] = frozenset(
        {
            LiveCommand.PING,
            LiveCommand.GET_ACTIVE_DOCUMENT,
            LiveCommand.GET_DOCUMENT_SVG,
            LiveCommand.RENDER_VIEW,
            LiveCommand.SET_VIEWPORT,
            LiveCommand.APPLY_TO_SELECTION,
        }
    )

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._connected = False

    # --- detection / lifecycle ----------------------------------------------

    @classmethod
    def _actions_list_reachable(cls, timeout_s: float) -> bool:
        """Whether ``org.gtk.Actions.List`` answers — a spawn-safe liveness probe.

        ``org.inkscape.Inkscape`` ships no DBus ``.service`` file, so calling it when no instance is
        running fails cleanly with ``ServiceUnknown`` and never spawns Inkscape. ``gdbus
        list-names`` does NOT list the (running) name on Inkscape 1.4.x, so a List call is the
        reliable detector (verified 2026-06-14).
        """
        binary = _gdbus()
        if binary is None:
            return False
        try:
            result = run_process(cls._actions_call_argv("List"), timeout_s=timeout_s)
        except ProcessError:
            return False
        return not result.timed_out and result.returncode == 0

    @staticmethod
    def _actions_call_argv(
        method: str, *params: str, object_path: str = INKSCAPE_OBJECT_PATH
    ) -> list[str]:
        """Build the ``gdbus call`` argv for an ``org.gtk.Actions`` method (arg-list, no shell)."""
        binary = _gdbus()
        if binary is None:  # pragma: no cover - callers check first
            raise LiveConnectionError("gdbus not available")
        return [
            binary,
            "call",
            "--session",
            "--dest",
            INKSCAPE_BUS_NAME,
            "--object-path",
            object_path,
            "--method",
            f"{GTK_ACTIONS_IFACE}.{method}",
            *params,
        ]

    @classmethod
    def probe(cls, settings: Settings) -> TransportProbe:
        commands = [c.value for c in sorted(cls.supported_commands)]
        if not _session_bus_present():
            return TransportProbe(
                name=cls.name,
                available=False,
                rank=cls.rank,
                supported_commands=commands,
                no_freeze=cls.no_freeze,
                detail="no session bus (DBUS_SESSION_BUS_ADDRESS unset) — Linux/BSD only",
            )
        if _gdbus() is None:
            return TransportProbe(
                name=cls.name,
                available=False,
                rank=cls.rank,
                supported_commands=commands,
                no_freeze=cls.no_freeze,
                detail="gdbus not available; cannot drive org.gtk.Actions",
            )
        live = cls._actions_list_reachable(settings.process_timeout_s)
        return TransportProbe(
            name=cls.name,
            available=live,
            rank=cls.rank,
            supported_commands=commands,
            no_freeze=cls.no_freeze,
            detail=(
                f"{INKSCAPE_BUS_NAME} reachable on session bus (no-freeze action path)"
                if live
                else f"session bus present; no {INKSCAPE_BUS_NAME} instance running"
            ),
        )

    def connect(self) -> None:
        if not _session_bus_present():
            raise LiveConnectionError("no session bus available")
        if _gdbus() is None:
            raise LiveConnectionError("gdbus not available")
        # org.gtk.Actions.List doubles as the connect/liveness handshake (proves the exported action
        # group is reachable). No action is activated.
        if not self._actions_list_reachable(self._settings.process_timeout_s):
            raise LiveConnectionError("Inkscape did not answer on the session bus")
        self._connected = True
        _logger.info(
            "live session connected",
            extra={"event": "process_exec", "transport": self.name, "no_freeze": True},
        )

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    # --- internal: activation + export helpers ------------------------------

    def _activate(
        self, action: str, parameter: str, *, object_path: str = INKSCAPE_OBJECT_PATH
    ) -> None:
        """Activate one GAction over ``org.gtk.Actions.Activate`` (no-freeze; arg-list/no shell)."""
        argv = self._actions_call_argv("Activate", action, parameter, "{}", object_path=object_path)
        try:
            result = run_process(argv, timeout_s=self._settings.process_timeout_s)
        except ProcessError as exc:
            raise LiveConnectionError("could not reach Inkscape on the session bus") from exc
        if result.timed_out or result.returncode != 0:
            # Stable, host-path-free message — never surface gdbus stderr (may carry detail).
            raise LiveConnectionError("Inkscape rejected a DBus action")

    def _export_to(
        self,
        out_path: Path,
        fmt: str,
        *,
        plain_svg: bool = False,
        area_page: bool = False,
        region: RenderRegion | None = None,
        dpi: float | None = None,
    ) -> None:
        """Drive the export-to-file action sequence so the live doc lands at ``out_path``.

        ``export-filename`` → ``export-type`` → (options) → ``export-do``. No-freeze (runs in the
        live process's main loop). Sets the instance's export options as a side effect (the action
        surface is stateful) but never mutates the document.
        """
        self._activate("export-filename", _variant_string(str(out_path)))
        self._activate("export-type", _variant_string(fmt))
        if plain_svg:
            self._activate("export-plain-svg", _variant_bool(True))
        if region is not None:
            # Finite-check here too: a future direct backend call could bypass `validate_region`,
            # and a bare "inf"/"nan" passes the quote guard but corrupts the export silently.
            coords = (region.x, region.y, region.width, region.height)
            if not all(math.isfinite(c) for c in coords):
                raise LiveError("non-finite render region rejected before DBus activation")
            x1 = region.x + region.width
            y1 = region.y + region.height
            area = f"{region.x}:{region.y}:{x1}:{y1}"
            self._activate("export-area", _variant_string(area))
        elif area_page:
            self._activate("export-area-page", _variant_bool(True))
        if dpi is not None:
            self._activate("export-dpi", _variant_double(dpi))
        self._activate("export-do", _variant_empty())

    def _export_document_bytes(self, fmt: str, **export_kwargs: object) -> bytes:
        """Export the live document to a temp file and return its bytes (cleaned up after read)."""
        tmpdir = Path(tempfile.mkdtemp(prefix="inkscape-mcp-live-"))
        # Fail fast with a clear message if the temp root itself carries characters that cannot be
        # embedded in a GVariant literal (e.g. a quote in TMPDIR), rather than a late opaque guard.
        if _GVARIANT_UNSAFE_RE.search(str(tmpdir)):
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise LiveError("temp directory path is not safe for a DBus export (check TMPDIR)")
        try:
            out = tmpdir / f"live.{fmt}"
            self._export_to(out, fmt, **export_kwargs)  # type: ignore[arg-type]
            if not out.exists():
                raise LiveConnectionError("Inkscape did not produce a DBus export")
            # Bound size BEFORE reading the whole file into memory (a runaway export can't exhaust
            # RAM ahead of the cap).
            if out.stat().st_size > self._settings.max_input_bytes:
                raise LiveError("live document export exceeds the input size cap")
            return out.read_bytes()
        finally:
            try:
                shutil.rmtree(tmpdir)
            except OSError:
                # Cleanup is best-effort, but surface it so operators can diagnose temp accumulation
                # (e.g. Inkscape wrote the export as a different uid).
                _logger.warning(
                    "could not remove live DBus export temp dir",
                    extra={"event": "file_io", "transport": "dbus"},
                )

    # --- read surface via export-to-file (E3-07) ----------------------------

    def get_active_document(self) -> LiveDocumentRef:
        """Identify the live document by exporting it and parsing the result (no GUI freeze).

        ``org.gtk.Actions`` returns nothing, so the document is exported to plain SVG and parsed
        through the normative safe parser. ``path`` stays null (the export does not carry the
        original on-disk path); ``name`` comes from ``sodipodi:docname`` and ``object_count`` from
        the parsed element count.
        """
        data = self._export_document_bytes("svg", plain_svg=True)
        try:
            tree = parse_svg_bytes(data)
        except UnsafeXMLError as exc:
            raise LiveError("live document could not be parsed safely") from exc
        root = tree.getroot()
        name = root.get(f"{{{_SODIPODI_NS}}}docname")
        object_count = sum(1 for _ in root.iter()) - 1  # exclude the root element itself
        return LiveDocumentRef(name=name, path=None, object_count=max(object_count, 0))

    def get_selection(self) -> LiveSelection:
        raise LiveCapabilityUnsupported(
            "DBus cannot read selection ids; use the extension-socket transport"
        )

    def inspect_selection(self) -> LiveSelectionInspection:
        raise LiveCapabilityUnsupported(
            "DBus cannot inspect the selection; use the extension-socket transport"
        )

    def get_document_svg(self) -> str:
        """Serialize the live document by exporting plain SVG to a temp file (no GUI freeze)."""
        data = self._export_document_bytes("svg", plain_svg=True)
        try:
            # Strict decode: Inkscape always emits UTF-8 SVG; a non-UTF-8 export is a fault to
            # surface, never silently mangle (this text is written to the workspace by sync).
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LiveError("live document export was not valid UTF-8") from exc

    def render_view(self, region: RenderRegion | None = None, scale: float | None = None) -> bytes:
        """Rasterize the live canvas by exporting a PNG over DBus (no GUI freeze).

        With ``region`` the export clips to that user-unit bbox (``export-area``); otherwise the
        page area is rendered. ``scale`` maps to the export dpi (``96 * scale``). Region/scale
        precision is best-effort vs the socket renderer; both are server-validated before crossing.
        """
        dpi = _BASE_DPI * scale if scale is not None else None
        return self._export_document_bytes("png", area_page=region is None, region=region, dpi=dpi)

    # --- viewport via the per-window action group (E3-07) --------------------

    def _active_window_path(self) -> str:
        """Resolve the live window's object path (``…/window/N``) for window-scoped actions.

        ``canvas-zoom-*`` are window actions, exported under the window object path rather than the
        app path. Picks the first window node from a recursive introspection (the common
        single-window case). Raises if no window is exported.
        """
        binary = _gdbus()
        if binary is None:  # pragma: no cover - callers check first
            raise LiveConnectionError("gdbus not available")
        try:
            result = run_process(
                [
                    binary,
                    "introspect",
                    "--session",
                    "--dest",
                    INKSCAPE_BUS_NAME,
                    "--object-path",
                    INKSCAPE_OBJECT_PATH,
                    "--recurse",
                ],
                timeout_s=self._settings.process_timeout_s,
            )
        except ProcessError as exc:
            raise LiveConnectionError("could not introspect the Inkscape window") from exc
        if result.timed_out or result.returncode != 0:
            raise LiveConnectionError("could not introspect the Inkscape window")
        match = re.search(r"/org/inkscape/Inkscape/window/\d+", result.stdout)
        if match is None:
            raise LiveCapabilityUnsupported("no Inkscape window is available for viewport control")
        return match.group(0)

    def set_viewport(
        self,
        *,
        mode: str,
        zoom: float | None = None,
        center: tuple[float, float] | None = None,
        dx: float | None = None,
        dy: float | None = None,
    ) -> LiveViewportResult:
        """Drive the canvas viewport via window ``canvas-zoom-*`` actions (no GUI freeze).

        ``fit_page`` → ``canvas-zoom-page``; ``fit_selection`` → ``canvas-zoom-selection``;
        ``zoom`` → ``canvas-zoom-absolute`` (center ignored — no absolute-recenter action). ``pan``
        has no faithful GAction over DBus, so it raises ``LiveCapabilityUnsupported`` (falls back to
        the socket backend). Params are already server-validated by ``live.edit.validate_viewport``.
        """
        window = self._active_window_path()
        if mode == "fit_page":
            self._activate("canvas-zoom-page", _variant_empty(), object_path=window)
            detail = "fit page (DBus, no-freeze)"
        elif mode == "fit_selection":
            self._activate("canvas-zoom-selection", _variant_empty(), object_path=window)
            detail = "fit selection (DBus, no-freeze)"
        elif mode == "zoom":
            if zoom is None:  # pragma: no cover - validate_viewport requires it
                raise EditError("zoom mode requires a zoom factor")
            self._activate("canvas-zoom-absolute", _variant_double(zoom), object_path=window)
            detail = "zoom (DBus, no-freeze)"
        else:  # pan and anything else
            raise LiveCapabilityUnsupported(
                "DBus cannot pan the viewport; use the extension-socket transport"
            )
        return LiveViewportResult(mode=mode, applied=True, detail=detail)

    # --- style/transform on the current selection (E3-07) -------------------

    def apply_to_selection(
        self, *, style: dict[str, str], transform: str | None
    ) -> LiveMutationResult:
        """Apply validated style props + a transform to the CURRENT selection (no GUI freeze).

        Style props go through ``object-set-property`` (one per property); the composed
        ``transform`` is re-parsed into ``transform-translate`` / ``transform-rotate`` /
        ``transform-scale`` activations. Each is an undoable Inkscape step in the live main loop.
        Operates on whatever is selected in the GUI — DBus cannot read the selection ids, so
        ``affected_ids`` stays empty (honest). Raises if the transform is not the exact
        server-generated grammar (defense).
        """
        applied: list[str] = []
        for prop, value in style.items():
            # Guard each field independently so the comma/space separator can never be smuggled in
            # via a property key, then build the action argument from the vetted parts.
            _guard_variant_str(prop)
            _guard_variant_str(value)
            self._activate("object-set-property", _variant_string(f"{prop}, {value}"))
            applied.append(prop)
        if transform is not None:
            applied.extend(self._apply_transform(transform))
        if not applied:  # pragma: no cover - the tool layer requires at least one input
            raise EditError("supply at least one style or transform parameter")
        return LiveMutationResult(
            affected_ids=[],
            count=0,
            detail=f"applied {', '.join(applied)} to the selection (DBus, no-freeze)",
            undo_friendly=True,
        )

    def _apply_transform(self, transform: str) -> list[str]:
        """Re-parse the server-composed transform into discrete ``transform-*`` activations."""
        applied: list[str] = []
        for part in transform.split(" "):
            if not part:
                continue
            if (m := _TRANSLATE_RE.match(part)) is not None:
                self._activate("transform-translate", _variant_string(f"{m['dx']},{m['dy']}"))
                applied.append("translate")
            elif (m := _ROTATE_RE.match(part)) is not None:
                self._activate("transform-rotate", _variant_string(m["deg"]))
                applied.append("rotate")
            elif (m := _SCALE_RE.match(part)) is not None:
                self._activate("transform-scale", _variant_string(m["f"]))
                applied.append("scale")
            else:
                raise LiveError("unsupported transform for the DBus action path")
        return applied
