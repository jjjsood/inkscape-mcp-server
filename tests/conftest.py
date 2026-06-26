"""Shared E3 live-mode test doubles: an in-memory `FakeTransport` and a real loopback mock helper.

The mock helper binds an actual ``127.0.0.1`` socket and speaks the fixed ``protocol.py`` schema,
so the real `ExtensionSocketTransport` client is exercised end-to-end without a running Inkscape.
"""

from __future__ import annotations

import os
import shutil
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, ClassVar

import pytest
from lxml import etree

from inkscape_mcp.config import (
    ENV_LIVE_ENABLED,
    ENV_RAW_ACTION_ENABLED,
    Settings,
    get_settings,
)
from inkscape_mcp.document.inspect import ObjectInfo
from inkscape_mcp.live.protocol import (
    LOOPBACK_HOST,
    PROTOCOL_VERSION,
    LiveCommand,
    build_error,
    build_ok,
    recv_message,
)
from inkscape_mcp.live.transport import (
    BBox,
    LiveDocumentRef,
    LiveMutationResult,
    LiveScene,
    LiveSelection,
    LiveSelectionInspection,
    LiveStateToken,
    LiveTransport,
    LiveViewportResult,
    RenderRegion,
    SceneCanvas,
    SceneSelectionItem,
    SceneViewport,
    TransportProbe,
)

#: Headless-first (architecture §3.2): the core suite must stay green on a runner with NO
#: Inkscape binary (e.g. a Windows/macOS CI job that does not install Inkscape). Tests that
#: genuinely drive the real binary carry ``@pytest.mark.inkscape``; this central hook SKIPS them
#: when ``inkscape`` is absent from PATH and RUNS them when it is present — no per-test ``skipif``
#: needed. The marker is also registered in ``pyproject.toml`` so collection emits no
#: ``PytestUnknownMarkWarning``. Live tests use in-memory fakes (below) and run everywhere.
_INKSCAPE_ON_PATH = shutil.which("inkscape") is not None

# E17-02: the suite's DEFAULT surface is the FULL surface. Progressive disclosure (E17-02) NARROWS
# `tools/list` when `live_enabled` / `raw_action_enabled` are off; many drift-guard tests (E16-01
# count, E17-01 annotations, llms.txt) register the surface at import and assert against the WHOLE
# catalog. Forcing both flags ON here — before any test module imports `register_tools` — makes the
# default surface the full 97 tools (today's behaviour). Tests that exercise the EXCLUSION path set
# the flags off explicitly + clear the settings cache + re-run `register_tools()`.
os.environ.setdefault(ENV_LIVE_ENABLED, "1")
os.environ.setdefault(ENV_RAW_ACTION_ENABLED, "1")
get_settings.cache_clear()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every ``@pytest.mark.inkscape`` test when no Inkscape binary is on PATH.

    Keeps the headless feature suite green independent of whether Inkscape is installed on the
    runner (CI matrix: Linux/macOS/Windows). When Inkscape IS present the marked tests run
    normally — the skip predicate is evaluated once at collection time.
    """
    if _INKSCAPE_ON_PATH:
        return
    skip_inkscape = pytest.mark.skip(reason="requires an Inkscape binary on PATH (none found)")
    for item in items:
        if "inkscape" in item.keywords:
            item.add_marker(skip_inkscape)


class FakeTransport(LiveTransport):
    """A fully in-memory transport for session/sync/render/tool tests (no real socket)."""

    name: ClassVar[str] = "fake"
    rank: ClassVar[int] = 99
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

    def __init__(self, svg: str = "", png: bytes = b"\x89PNG\r\n\x1a\nFAKE") -> None:
        self._connected = False
        self._svg = svg or (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" '
            'width="10" height="10"><rect id="r1" x="1" y="1" width="2" height="2"/></svg>'
        )
        self._png = png
        #: Last render request seen, so view tests can assert region/scale crossed the boundary.
        self.last_render: tuple[RenderRegion | None, float | None] | None = None
        #: Last viewport request seen, so view tests can assert the mode/params.
        self.last_viewport: dict[str, object] | None = None
        #: Mutable RAW state-token components (E8-03). Change tests mutate these and assert the
        #: right delta flag fires; the server hashes them via `token_from_result` on each read.
        self.state_revision: str = "rev-0"
        self.state_selection: list[str] = ["r1"]
        self.state_viewport: dict[str, object] = {"zoom": 1.0, "center": [5.0, 5.0]}

    @classmethod
    def probe(cls, settings: Settings) -> TransportProbe:
        return TransportProbe(
            name=cls.name,
            available=True,
            rank=cls.rank,
            supported_commands=[c.value for c in cls.supported_commands],
            detail="fake transport",
        )

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_active_document(self) -> LiveDocumentRef:
        return LiveDocumentRef(name="live.svg", path="/tmp/live.svg", object_count=1)

    def get_selection(self) -> LiveSelection:
        return LiveSelection(object_ids=["r1"], count=1)

    def inspect_selection(self) -> LiveSelectionInspection:
        objs = [ObjectInfo(id="r1", tag="rect", label=None, has_style=False)]
        return LiveSelectionInspection(objects=objs, count=1)

    def get_document_svg(self) -> str:
        return self._svg

    def render_view(self, region: RenderRegion | None = None, scale: float | None = None) -> bytes:
        self.last_render = (region, scale)
        if region is not None or scale is not None:
            return self._png + b"-REGION"
        return self._png

    def set_viewport(
        self,
        *,
        mode: str,
        zoom: float | None = None,
        center: tuple[float, float] | None = None,
        dx: float | None = None,
        dy: float | None = None,
    ) -> LiveViewportResult:
        self.last_viewport = {
            "mode": mode,
            "zoom": zoom,
            "center": center,
            "dx": dx,
            "dy": dy,
        }
        return LiveViewportResult(mode=mode, applied=True, detail=f"viewport {mode}")

    def get_scene(self) -> LiveScene:
        return LiveScene(
            active_document=self.get_active_document(),
            selection=[SceneSelectionItem(id="r1", bbox=BBox(x=1, y=1, width=2, height=2))],
            selection_count=1,
            viewport=SceneViewport(zoom=1.0, center=(5.0, 5.0)),
            canvas=SceneCanvas(width=10.0, height=10.0, units=None),
            visible_objects=[ObjectInfo(id="r1", tag="rect", label=None, has_style=False)],
            object_count=1,
        )

    def get_state_token(self) -> tuple[LiveStateToken, list[str]]:
        # Hash the mutable raw components server-side exactly as the real socket backend does.
        from inkscape_mcp.live.events import token_from_result

        return token_from_result(
            {
                "revision": self.state_revision,
                "selection": list(self.state_selection),
                "viewport": dict(self.state_viewport),
            }
        )

    # --- Write surface: mutate the in-memory SVG so a later sync reflects the change ---

    def _root(self) -> etree._Element:
        return etree.fromstring(self._svg.encode("utf-8"))

    def _find(self, root: etree._Element, oid: str) -> etree._Element | None:
        for el in root.iter():
            if isinstance(el.tag, str) and el.get("id") == oid:
                return el
        return None

    def apply_to_selection(
        self, *, style: dict[str, str], transform: str | None
    ) -> LiveMutationResult:
        root = self._root()
        affected: list[str] = []
        for oid in self.get_selection().object_ids:
            elem = self._find(root, oid)
            if elem is None:
                continue
            for key, val in style.items():
                elem.set(key, val)
            if transform:
                elem.set("transform", transform)
            affected.append(oid)
        self._svg = etree.tostring(root).decode("utf-8")
        return LiveMutationResult(
            affected_ids=affected, count=len(affected), detail="applied", undo_friendly=True
        )

    def insert_svg(self, svg_fragment: str) -> LiveMutationResult:
        root = self._root()
        frag = etree.fromstring(
            f'<g xmlns="http://www.w3.org/2000/svg">{svg_fragment}</g>'.encode()
        )
        ids = [c.get("id") for c in frag if isinstance(c.tag, str) and c.get("id")]
        for child in list(frag):
            root.append(child)
        self._svg = etree.tostring(root).decode("utf-8")
        return LiveMutationResult(
            affected_ids=[i for i in ids if i],
            count=len(ids),
            detail="inserted",
            undo_friendly=True,
        )

    def set_selected_text(self, text: str) -> LiveMutationResult:
        return LiveMutationResult(
            affected_ids=self.get_selection().object_ids, count=1, detail="text", undo_friendly=True
        )

    def export_selection(self) -> bytes:
        return self._png


# --- Real loopback mock helper (exercises the socket client) ---------------

Handler = Callable[[str, dict[str, Any]], dict[str, Any]]


def default_handler(cmd: str, params: dict[str, Any]) -> dict[str, Any]:
    """Canned responses matching the fixed schema for each read command."""
    if cmd == LiveCommand.PING:
        return {"pong": True}
    if cmd == LiveCommand.GET_ACTIVE_DOCUMENT:
        return {"name": "live.svg", "path": "/home/u/live.svg", "object_count": 2}
    if cmd == LiveCommand.GET_SELECTION:
        return {"object_ids": ["a", "b"]}
    if cmd == LiveCommand.INSPECT_SELECTION:
        return {"objects": [{"id": "a", "tag": "rect", "label": None, "has_style": True}]}
    if cmd == LiveCommand.GET_DOCUMENT_SVG:
        return {"svg": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 4"/>'}
    if cmd == LiveCommand.GET_STATE_TOKEN:
        return {
            "revision": "rev-abc",
            "selection": ["a", "b"],
            "viewport": {"zoom": 1.0, "center": [2.0, 2.0]},
        }
    raise KeyError(cmd)


class MockHelperServer:
    """Loopback server speaking the fixed protocol: handshake + dispatch on a background thread."""

    def __init__(self, token: str, handler: Handler | None = None) -> None:
        self.token = token
        self.handler = handler or default_handler
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind((LOOPBACK_HOST, 0))
        self._sock.listen(1)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            try:
                first = recv_message(conn)
            except Exception:
                return
            if first.get("cmd") != LiveCommand.HELLO or first.get("token") != self.token:
                conn.sendall(build_error("unauthorized"))
                return
            conn.sendall(
                build_ok(
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "capabilities": [c.value for c in LiveCommand],
                        "inkscape_version": "1.4.3",
                    }
                )
            )
            while True:
                try:
                    msg = recv_message(conn)
                except Exception:
                    return
                if msg.get("token") != self.token:
                    conn.sendall(build_error("unauthorized"))
                    return
                cmd = str(msg.get("cmd", ""))
                try:
                    result = self.handler(cmd, msg.get("params", {}))
                except KeyError:
                    conn.sendall(build_error("unknown command"))
                    continue
                conn.sendall(build_ok(result))

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


@contextmanager
def mock_helper(token: str = "secret-token", handler: Handler | None = None) -> Iterator[int]:
    """Start a `MockHelperServer`, yield its loopback port, and tear it down afterwards."""
    server = MockHelperServer(token, handler)
    server.start()
    try:
        yield server.port
    finally:
        server.close()
