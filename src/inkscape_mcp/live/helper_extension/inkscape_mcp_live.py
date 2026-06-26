#!/usr/bin/env python3
"""inkscape-mcp live helper extension (E3-02) — runs INSIDE Inkscape.

Fixed-purpose `inkex` extension that bridges a running Inkscape document to the inkscape-mcp
server over a LOOPBACK-ONLY socket using a fixed, versioned semantic command schema. It exposes
NO arbitrary code execution and NO raw Action passthrough (ADR-003): only the read commands the
server's live tools need.

Lifecycle: Inkscape runs this extension on the active document; it binds ``127.0.0.1`` on an
ephemeral port, mints a random session token, and advertises both by writing a rendezvous file
(0600) the server discovers. It then serves a single client until that client disconnects or an
idle timeout elapses, after which the extension returns control to Inkscape.

IMPORTANT: this file runs under Inkscape's bundled Python with `inkex`, so it CANNOT import the
inkscape-mcp server package. The protocol constants below MUST stay in lock-step with
``inkscape_mcp/live/protocol.py`` — bump PROTOCOL_VERSION on both sides together.

KNOWN LIMITATION: an `inkex` extension runs on the document snapshot Inkscape hands it at launch;
it reflects the selection/document at invocation time, not subsequent live GUI edits. Full live
selection tracking is a later epic.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import inkex  # type: ignore[import-not-found]  # provided by Inkscape at runtime

# --- Protocol (mirror of inkscape_mcp/live/protocol.py — keep in lock-step) ---
PROTOCOL_VERSION = 5
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
LOOPBACK_HOST = "127.0.0.1"
RENDEZVOUS_FILENAME = "inkscape-mcp-live.json"
IDLE_TIMEOUT_S = 120.0

CMD_HELLO = "hello"
CMD_PING = "ping"
CMD_GET_ACTIVE_DOCUMENT = "get_active_document"
CMD_GET_SELECTION = "get_selection"
CMD_INSPECT_SELECTION = "inspect_selection"
CMD_GET_DOCUMENT_SVG = "get_document_svg"
CMD_RENDER_VIEW = "render_view"
# E4 semantic WRITE commands (mutating; the server approval-gates each before sending).
CMD_APPLY_TO_SELECTION = "apply_to_selection"
CMD_INSERT_SVG = "insert_svg"
CMD_SET_SELECTED_TEXT = "set_selected_text"
CMD_EXPORT_SELECTION = "export_selection"
# E8 view-only commands (non-mutating; the server treats these as low risk, no Operation Record).
CMD_SET_VIEWPORT = "set_viewport"
CMD_GET_SCENE = "get_scene"  # structured perception: canvas + selection bboxes + visible objects
CMD_GET_STATE_TOKEN = "get_state_token"  # CHEAP change marker: revision + selection + viewport

#: Viewport modes the server may request (fixed semantic verbs — no raw Action passthrough).
_VIEWPORT_MODES = {"zoom", "pan", "fit_selection", "fit_page"}

CAPABILITIES = [
    CMD_PING,
    CMD_GET_ACTIVE_DOCUMENT,
    CMD_GET_SELECTION,
    CMD_INSPECT_SELECTION,
    CMD_GET_DOCUMENT_SVG,
    CMD_RENDER_VIEW,
    CMD_APPLY_TO_SELECTION,
    CMD_INSERT_SVG,
    CMD_SET_SELECTED_TEXT,
    CMD_EXPORT_SELECTION,
    CMD_SET_VIEWPORT,
    CMD_GET_SCENE,
    CMD_GET_STATE_TOKEN,
]

#: Local names of SVG elements whose primary purpose is to carry text content (set-selected-text).
_TEXT_TAGS = {"text", "tspan", "textPath", "tref", "flowRoot", "flowPara", "flowSpan"}

SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"
_NON_OBJECT_TAGS = {"svg", "defs", "metadata", "style", "title", "desc", "namedview"}


def _rendezvous_path() -> Path:
    """Where to advertise the socket. Mirrors the server's discovery order (env → temp)."""
    override = os.environ.get("INKSCAPE_MCP_LIVE_RENDEZVOUS", "").strip()
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / RENDEZVOUS_FILENAME


def _send(sock: socket.socket, obj: dict[str, Any]) -> None:
    sock.sendall(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")


def _recv(sock: socket.socket) -> dict[str, Any] | None:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return None
        nl = chunk.find(b"\n")
        if nl != -1:
            chunks.append(chunk[: nl + 1])
            total += nl + 1
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_MESSAGE_BYTES:
            return None
    if total > MAX_MESSAGE_BYTES:
        return None
    raw = b"".join(chunks).rstrip(b"\n")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _local_name(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _fmt_num(v: float) -> str:
    """Format a float for an Inkscape CLI flag in DECIMAL notation (never scientific).

    Inkscape's ``--export-area`` / ``--export-dpi`` parsers reject scientific notation, which
    ``str(float)`` can emit at extreme magnitudes. Values here are already finite + bounded, but
    formatting decimally keeps the flag valid regardless of future bound changes.
    """
    return f"{v:.6f}".rstrip("0").rstrip(".")


class InkscapeMcpLive(inkex.EffectExtension):  # type: ignore[misc]
    """Serve the active document to the inkscape-mcp server over a loopback socket."""

    def effect(self) -> None:
        token = os.urandom(16).hex()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((LOOPBACK_HOST, 0))  # loopback-only; ephemeral port
        server.listen(1)
        port = server.getsockname()[1]
        self._write_rendezvous(port, token)
        server.settimeout(IDLE_TIMEOUT_S)
        try:
            conn, _addr = server.accept()
        except OSError:
            self._clear_rendezvous()
            server.close()
            return
        try:
            self._serve(conn, token)
        finally:
            conn.close()
            server.close()
            self._clear_rendezvous()

    def _write_rendezvous(self, port: int, token: str) -> None:
        path = _rendezvous_path()
        payload = {
            "port": port,
            "token": token,
            "protocol_version": PROTOCOL_VERSION,
            "pid": os.getpid(),
        }
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def _clear_rendezvous(self) -> None:
        try:
            _rendezvous_path().unlink()
        except OSError:
            pass

    def _serve(self, conn: socket.socket, token: str) -> None:
        conn.settimeout(IDLE_TIMEOUT_S)
        # Handshake first: the opening message MUST be a hello carrying the session token.
        first = _recv(conn)
        if not first or first.get("cmd") != CMD_HELLO or first.get("token") != token:
            _send(conn, {"v": PROTOCOL_VERSION, "ok": False, "error": "unauthorized"})
            return
        _send(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "ok": True,
                "result": {
                    "protocol_version": PROTOCOL_VERSION,
                    "capabilities": CAPABILITIES,
                    "inkscape_version": getattr(inkex, "__version__", None),
                },
            },
        )
        while True:
            msg = _recv(conn)
            if msg is None:
                return
            if msg.get("token") != token:
                _send(conn, {"v": PROTOCOL_VERSION, "ok": False, "error": "unauthorized"})
                return
            params = msg.get("params")
            self._dispatch(
                conn, str(msg.get("cmd", "")), params if isinstance(params, dict) else {}
            )

    def _dispatch(self, conn: socket.socket, cmd: str, params: dict[str, Any]) -> None:
        try:
            if cmd == CMD_PING:
                result: dict[str, Any] = {"pong": True}
            elif cmd == CMD_GET_ACTIVE_DOCUMENT:
                result = self._active_document()
            elif cmd == CMD_GET_SELECTION:
                result = {"object_ids": self._selection_ids()}
            elif cmd == CMD_INSPECT_SELECTION:
                result = {"objects": self._inspect_selection()}
            elif cmd == CMD_GET_DOCUMENT_SVG:
                result = {"svg": self._document_svg()}
            elif cmd == CMD_RENDER_VIEW:
                result = {
                    "png_base64": self._render_png(
                        None, region=params.get("region"), scale=params.get("scale")
                    )
                }
            elif cmd == CMD_SET_VIEWPORT:
                result = self._set_viewport(params)
            elif cmd == CMD_GET_SCENE:
                result = self._get_scene()
            elif cmd == CMD_GET_STATE_TOKEN:
                result = self._get_state_token()
            elif cmd == CMD_APPLY_TO_SELECTION:
                result = self._apply_to_selection(params)
            elif cmd == CMD_INSERT_SVG:
                result = self._insert_svg(params)
            elif cmd == CMD_SET_SELECTED_TEXT:
                result = self._set_selected_text(params)
            elif cmd == CMD_EXPORT_SELECTION:
                result = {"png_base64": self._render_png(self._selection_ids())}
            else:
                _send(conn, {"v": PROTOCOL_VERSION, "ok": False, "error": "unknown command"})
                return
        except Exception as exc:  # never leak a traceback (or an OS path in str(exc)) over the wire
            _send(
                conn,
                {
                    "v": PROTOCOL_VERSION,
                    "ok": False,
                    "error": f"command failed: {type(exc).__name__}",
                },
            )
            return
        _send(conn, {"v": PROTOCOL_VERSION, "ok": True, "result": result})

    # --- Read implementations (operate on the document Inkscape handed this run) ---

    def _active_document(self) -> dict[str, Any]:
        path = None
        try:
            path = self.document_path()  # type: ignore[attr-defined]
        except Exception:
            path = None
        name = Path(path).name if path else None
        return {
            "name": name,
            "path": path or None,
            "object_count": sum(1 for _ in self._iter_objects()),
        }

    def _selection_ids(self) -> list[str]:
        ids: list[str] = []
        try:
            for elem in self.svg.selection.values():  # type: ignore[attr-defined]
                el_id = elem.get("id")
                if el_id:
                    ids.append(el_id)
        except Exception:
            ids = list(getattr(self.options, "ids", []) or [])
        return ids

    def _inspect_selection(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        wanted = set(self._selection_ids())
        for elem in self._iter_objects():
            el_id = elem.get("id")
            if el_id not in wanted:
                continue
            style = elem.get("style")
            has_style = bool(style) or bool(elem.get("fill")) or bool(elem.get("stroke"))
            out.append(
                {
                    "id": el_id,
                    "tag": _local_name(elem.tag),
                    "label": elem.get(f"{{{INKSCAPE_NS}}}label"),
                    "has_style": has_style,
                }
            )
        return out

    def _document_svg(self) -> str:
        return self.svg.tostring().decode("utf-8")  # type: ignore[attr-defined]

    def _render_png(
        self,
        only_ids: list[str] | None,
        region: Any = None,
        scale: Any = None,
    ) -> str:
        """Rasterize the document via the Inkscape CLI (transport render, never a window grab).

        With `only_ids` it clips to those objects (selection export). With `region` (a 4-number
        ``[x, y, w, h]`` in user units, server-validated) it exports just that area; otherwise the
        whole page. `scale` (>0, server-validated) shrinks/enlarges the raster via export width.
        All flags are fixed and every numeric is re-checked here before reaching the arg list.
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "doc.svg"
            out = Path(tmp) / "view.png"
            src.write_bytes(self.svg.tostring())  # type: ignore[attr-defined]
            argv = ["inkscape", str(src), "--export-type=png", f"--export-filename={out}"]
            area = self._region_area(region)
            if only_ids:
                # Clip to the selection's bounding boxes (fixed flags; ids never reach a shell).
                argv += [f"--export-id={','.join(only_ids)}", "--export-id-only"]
            elif area is not None:
                x, y, w, h = area
                argv.append(
                    f"--export-area={_fmt_num(x)}:{_fmt_num(y)}:{_fmt_num(x + w)}:{_fmt_num(y + h)}"
                )
            else:
                argv.append("--export-area-page")
            sc = self._render_scale(scale)
            if sc is not None and area is not None:
                # Drive output width from the region width so scale is deterministic + bounded.
                px = max(1, int(round(area[2] * sc)))
                argv.append(f"--export-width={px}")
            elif sc is not None:
                argv.append(f"--export-dpi={_fmt_num(max(1.0, 96.0 * sc))}")
            subprocess.run(  # noqa: S603 - fixed arg list, no shell
                argv,
                check=False,
                capture_output=True,
                timeout=IDLE_TIMEOUT_S,
            )
            if not out.is_file():
                raise RuntimeError("render produced no output")
            return base64.b64encode(out.read_bytes()).decode("ascii")

    @staticmethod
    def _region_area(region: Any) -> tuple[float, float, float, float] | None:
        """Re-validate a render region ``[x, y, w, h]`` (finite; w/h positive) inside the helper."""
        if region is None:
            return None
        if not isinstance(region, (list, tuple)) or len(region) != 4:
            raise ValueError("region must be [x, y, w, h]")
        nums = []
        for value in region:
            num = float(value)
            if not (num == num and num not in (float("inf"), float("-inf"))):
                raise ValueError("region values must be finite")
            nums.append(num)
        if nums[2] <= 0 or nums[3] <= 0:
            raise ValueError("region width/height must be positive")
        return (nums[0], nums[1], nums[2], nums[3])

    @staticmethod
    def _render_scale(scale: Any) -> float | None:
        """Re-validate a render scale factor (finite, positive) inside the helper."""
        if scale is None:
            return None
        num = float(scale)
        if not (num == num and num not in (float("inf"), float("-inf"))) or num <= 0:
            raise ValueError("scale must be a positive finite number")
        return num

    def _set_viewport(self, params: dict[str, Any]) -> dict[str, Any]:
        """Drive the canvas viewport (zoom / pan / fit-to-selection / fit-to-page).

        VIEW-ONLY: this never alters the document — it only changes how the canvas is displayed,
        so the server records no Operation Record. An `inkex` effect extension runs on a document
        snapshot and cannot move the live GUI viewport directly, so this acknowledges the
        server-validated, bounded request as a no-op-on-document and reports the applied mode. A
        DBus/live fast-path that can drive the GUI viewport overrides this server-side.
        """
        mode = params.get("mode")
        if mode not in _VIEWPORT_MODES:
            raise ValueError("unknown viewport mode")
        return {"mode": mode, "applied": True}

    def _get_scene(self) -> dict[str, Any]:
        """Build the structured scene (E8-02): selection bboxes + canvas + visible-object summary.

        VIEW-ONLY perception: this reads the document snapshot Inkscape handed this run and never
        mutates it. The viewport is reported as null — an `inkex` effect extension runs on a
        document snapshot and cannot observe the live GUI zoom/center (a DBus/live fast-path that
        can would fill it in server-side). Every bounding box is best-effort: a node whose bbox
        Inkscape cannot compute is reported with a null bbox rather than failing the whole scene.
        """
        wanted = set(self._selection_ids())
        selection: list[dict[str, Any]] = []
        visible_objects: list[dict[str, Any]] = []
        for elem in self._iter_objects():
            el_id = elem.get("id")
            style = elem.get("style")
            has_style = bool(style) or bool(elem.get("fill")) or bool(elem.get("stroke"))
            visible_objects.append(
                {
                    "id": el_id,
                    "tag": _local_name(elem.tag),
                    "label": elem.get(f"{{{INKSCAPE_NS}}}label"),
                    "has_style": has_style,
                }
            )
            if el_id is not None and el_id in wanted:
                selection.append({"id": el_id, "bbox": self._element_bbox(elem)})
        return {
            "selection": selection,
            "viewport": {"zoom": None, "center": None, "visible_region": None},
            "canvas": self._canvas_size(),
            "visible_objects": visible_objects,
        }

    def _get_state_token(self) -> dict[str, Any]:
        """Build the CHEAP change-detection marker (E8-03): revision + selection + viewport.

        Deliberately cheap: NO PNG render and NO full-document payload crosses the wire for a poll —
        the server hashes these small components itself. The ``revision`` is a content hash of the
        serialized document (a stable marker that changes whenever the document content does), the
        ``selection`` is the current selection ids, and the ``viewport`` is reported null.

        KNOWN LIMITATION (mirrors get_scene): an ``inkex`` effect extension runs on the document
        snapshot Inkscape handed this run and cannot observe the live GUI zoom/center, so the
        viewport is null here. The token mechanism is transport-agnostic — a fast-path that reads the
        live document each poll fills these from current state and detects the user's GUI edits.
        """
        import hashlib

        try:
            raw = self.svg.tostring()  # type: ignore[attr-defined]
        except Exception:
            raw = b""
        revision = hashlib.sha256(raw).hexdigest()
        return {
            "revision": revision,
            "selection": self._selection_ids(),
            "viewport": {"zoom": None, "center": None},
        }

    @staticmethod
    def _element_bbox(elem: Any) -> list[float] | None:
        """Best-effort bounding box ``[x, y, w, h]`` for an element, or None if uncomputable."""
        try:
            box = elem.bounding_box()
        except Exception:
            return None
        if box is None:
            return None
        try:
            vals = [float(box.left), float(box.top), float(box.width), float(box.height)]
        except Exception:
            return None
        # Never emit non-finite numbers over the wire (defence in depth; server also drops them).
        return vals if all(math.isfinite(v) for v in vals) else None

    def _canvas_size(self) -> dict[str, Any]:
        """Document canvas size in user units from width/height or the viewBox (best-effort)."""
        root = self.document.getroot()  # type: ignore[attr-defined]
        width = self._user_unit(root.get("width"))
        height = self._user_unit(root.get("height"))
        if width is None or height is None:
            vb = root.get("viewBox")
            if vb:
                parts = vb.replace(",", " ").split()
                if len(parts) == 4:
                    try:
                        vb_w = float(parts[2])
                        vb_h = float(parts[3])
                    except ValueError:
                        vb_w = vb_h = float("nan")
                    if width is None and math.isfinite(vb_w):
                        width = vb_w
                    if height is None and math.isfinite(vb_h):
                        height = vb_h
        units = root.get(f"{{{INKSCAPE_NS}}}document-units")
        return {"width": width, "height": height, "units": units}

    @staticmethod
    def _user_unit(value: Any) -> float | None:
        """Parse a leading numeric (dropping a unit suffix) from a width/height attr, or None."""
        if not isinstance(value, str) or not value.strip():
            return None
        match = re.match(r"\s*([-+]?[0-9]*\.?[0-9]+)", value)
        if not match:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    # --- Write implementations (E4; server has already validated every value) ---

    def _selected_elements(self) -> list[Any]:
        wanted = set(self._selection_ids())
        return [el for el in self._iter_objects() if el.get("id") in wanted]

    def _apply_to_selection(self, params: dict[str, Any]) -> dict[str, Any]:
        style = params.get("style") or {}
        transform = params.get("transform")
        affected: list[str] = []
        for elem in self._selected_elements():
            if isinstance(style, dict) and style:
                self._merge_style(elem, style)
            if isinstance(transform, str) and transform:
                existing = elem.get("transform")
                elem.set("transform", f"{transform} {existing}".strip() if existing else transform)
            el_id = elem.get("id")
            if el_id:
                affected.append(el_id)
        return {
            "affected_ids": affected,
            "detail": f"applied style/transform to {len(affected)} object(s)",
            "undo_friendly": True,
        }

    def _set_selected_text(self, params: dict[str, Any]) -> dict[str, Any]:
        text = params.get("text")
        if not isinstance(text, str):
            raise ValueError("text param missing")
        affected: list[str] = []
        for elem in self._selected_elements():
            if _local_name(elem.tag) not in _TEXT_TAGS:
                continue
            for child in list(elem):
                elem.remove(child)
            elem.text = text
            el_id = elem.get("id")
            if el_id:
                affected.append(el_id)
        return {
            "affected_ids": affected,
            "detail": f"set text on {len(affected)} object(s)",
            "undo_friendly": True,
        }

    def _insert_svg(self, params: dict[str, Any]) -> dict[str, Any]:
        fragment = params.get("svg")
        if not isinstance(fragment, str) or not fragment.strip():
            raise ValueError("svg param missing")
        # The server already safe-parsed this fragment; parse here only to graft the nodes in.
        # Defence in depth: use a hardened parser so a fragment that slipped a DTD/entity past the
        # server can never trigger XXE / billion-laughs inside Inkscape's interpreter.
        from lxml import etree as _etree

        wrapped = (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:xlink="http://www.w3.org/1999/xlink">' + fragment + "</svg>"
        )
        safe_parser = _etree.XMLParser(
            resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False
        )
        parsed = _etree.fromstring(wrapped.encode("utf-8"), safe_parser)  # noqa: S320
        layer = self.svg.get_current_layer()  # type: ignore[attr-defined]
        target = layer if layer is not None else self.document.getroot()  # type: ignore[attr-defined]
        affected: list[str] = []
        for child in list(parsed):
            target.append(child)
            el_id = child.get("id")
            if el_id:
                affected.append(el_id)
        return {
            "affected_ids": affected,
            "detail": f"inserted {len(affected)} object(s)",
            "undo_friendly": True,
        }

    def _merge_style(self, elem: Any, style: dict[str, Any]) -> None:
        """Merge validated CSS props into the element's inline ``style`` (drops same-named attrs)."""
        decls: dict[str, str] = {}
        raw = elem.get("style")
        if raw:
            for part in raw.split(";"):
                if ":" in part:
                    key, _, val = part.partition(":")
                    key = key.strip()
                    if key:
                        decls[key] = val.strip()
        for key, val in style.items():
            decls[str(key)] = str(val)
            if str(key) in elem.attrib:
                del elem.attrib[str(key)]
        elem.set("style", ";".join(f"{k}:{v}" for k, v in decls.items()))

    def _iter_objects(self) -> list[Any]:
        root = self.document.getroot()  # type: ignore[attr-defined]
        return [
            el
            for el in root.iter()
            if isinstance(el.tag, str) and _local_name(el.tag) not in _NON_OBJECT_TAGS
        ]


if __name__ == "__main__":
    InkscapeMcpLive().run()
    sys.exit(0)
