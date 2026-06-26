"""DBus backend tests (+): Linux-only availability + the no-freeze action path.

The backend drives ``org.gtk.Actions`` via the ``gdbus`` CLI. These tests replace that CLI with a
fake ``run_process`` so the whole no-freeze surface (export-to-file reads, viewport, style/transform
writes) is exercised cross-platform without a running Inkscape — the exact gdbus argv and the
GVariant encoding are asserted, and the export-to-file workaround round-trips through a real temp
file the fake writes. One self-skipping integration test drives a genuine live instance when one is
on the session bus.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from inkscape_mcp.config import get_settings
from inkscape_mcp.live import dbus_backend
from inkscape_mcp.live.dbus_backend import (
    DBusTransport,
    _variant_bool,
    _variant_double,
    _variant_string,
)
from inkscape_mcp.live.protocol import LiveCommand
from inkscape_mcp.live.transport import (
    LiveCapabilityUnsupported,
    LiveError,
    RenderRegion,
)
from inkscape_mcp.workspace.subprocess_exec import ProcessResult


@pytest.fixture
def settings() -> object:
    get_settings.cache_clear()
    return get_settings()


# --- fake gdbus ------------------------------------------------------------


_PATH_RE = re.compile(r"\[<'([^']*)'>\]")


class FakeGdbus:
    """A stateful fake for ``run_process`` standing in for the ``gdbus`` CLI.

    Records every Activate ``(action, param)`` so tests can assert the exact sequence. Emulates the
    export-to-file workaround: an ``export-filename`` Activate captures the target path and the
    following ``export-do`` writes ``export_payload`` there, so read methods round-trip through a
    real file. Introspection returns a canned window path.
    """

    def __init__(self, *, export_payload: bytes = b"", list_ok: bool = True) -> None:
        self.export_payload = export_payload
        self.list_ok = list_ok
        self.activations: list[tuple[str, str]] = []
        self.object_paths: list[str] = []
        self._pending_export: Path | None = None

    def __call__(self, args: list[str], timeout_s: float | None = None) -> ProcessResult:
        def ok(stdout: str = "()\n", rc: int = 0) -> ProcessResult:
            return ProcessResult(
                args=args, returncode=rc, stdout=stdout, stderr="", duration_s=0.0, timed_out=False
            )

        method = args[args.index("--method") + 1] if "--method" in args else ""
        object_path = args[args.index("--object-path") + 1] if "--object-path" in args else ""

        if "introspect" in args:
            return ok(stdout="  node /org/inkscape/Inkscape/window/1 {\n")
        if method.endswith(".List"):
            return ok(stdout="(['ping'],)\n", rc=0 if self.list_ok else 1)
        if method.endswith(".Activate"):
            action = args[args.index("--method") + 1 + 1]
            param = args[args.index("--method") + 1 + 2]
            self.activations.append((action, param))
            self.object_paths.append(object_path)
            if action == "export-filename":
                m = _PATH_RE.search(param)
                self._pending_export = Path(m.group(1)) if m else None
            elif action == "export-do" and self._pending_export is not None:
                self._pending_export.write_bytes(self.export_payload)
            return ok()
        return ok()


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeGdbus) -> None:
    monkeypatch.setattr(dbus_backend, "_session_bus_present", lambda: True)
    monkeypatch.setattr(dbus_backend, "_gdbus", lambda: "gdbus")
    monkeypatch.setattr(dbus_backend, "run_process", fake)


def _actions(fake: FakeGdbus) -> list[str]:
    return [a for a, _ in fake.activations]


# --- capability advertisement ----------------------------------------------


def test_no_freeze_flag_is_set() -> None:
    assert DBusTransport.no_freeze is True


def test_capability_set_is_action_expressible_surface() -> None:
    # Honest set: liveness + export-based reads + viewport + apply-to-selection. NOT selection-id
    # reads or insert/set-text (no faithful GAction).
    assert DBusTransport.supported_commands == frozenset(
        {
            LiveCommand.PING,
            LiveCommand.GET_ACTIVE_DOCUMENT,
            LiveCommand.GET_DOCUMENT_SVG,
            LiveCommand.RENDER_VIEW,
            LiveCommand.SET_VIEWPORT,
            LiveCommand.APPLY_TO_SELECTION,
        }
    )
    for absent in (
        LiveCommand.GET_SELECTION,
        LiveCommand.INSPECT_SELECTION,
        LiveCommand.INSERT_SVG,
        LiveCommand.SET_SELECTED_TEXT,
    ):
        assert absent not in DBusTransport.supported_commands


# --- detection -------------------------------------------------------------


def test_unavailable_without_session_bus(settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dbus_backend, "_session_bus_present", lambda: False)
    probe = DBusTransport.probe(settings)  # type: ignore[arg-type]
    assert probe.available is False
    assert probe.no_freeze is True
    assert "session bus" in probe.detail


def test_probe_unavailable_without_gdbus(settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dbus_backend, "_session_bus_present", lambda: True)
    monkeypatch.setattr(dbus_backend, "_gdbus", lambda: None)
    probe = DBusTransport.probe(settings)  # type: ignore[arg-type]
    assert probe.available is False
    assert "gdbus" in probe.detail


def test_probe_available_uses_actions_list(
    settings: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGdbus(list_ok=True)
    _install(monkeypatch, fake)
    probe = DBusTransport.probe(settings)  # type: ignore[arg-type]
    assert probe.available is True
    assert probe.no_freeze is True
    # Detection is via org.gtk.Actions.List (spawn-safe), not gdbus list-names — and a probe must
    # never activate an action.
    assert fake.activations == []


def test_probe_unavailable_when_list_fails(
    settings: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ServiceUnknown (no running instance) ⇒ List returns non-zero ⇒ unavailable, no spawn.
    fake = FakeGdbus(list_ok=False)
    _install(monkeypatch, fake)
    probe = DBusTransport.probe(settings)  # type: ignore[arg-type]
    assert probe.available is False
    assert "no" in probe.detail


# --- export-to-file structured read (workaround) ---------------------


def test_get_document_svg_via_export(settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect id="r1"/></svg>'
    fake = FakeGdbus(export_payload=svg)
    _install(monkeypatch, fake)
    transport = DBusTransport(settings)  # type: ignore[arg-type]
    out = transport.get_document_svg()
    assert "rect" in out
    acts = _actions(fake)
    assert acts == ["export-filename", "export-type", "export-plain-svg", "export-do"]


def test_get_active_document_parses_export(
    settings: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    svg_text = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.0.dtd" '
        'sodipodi:docname="poster.svg">'
        '<rect id="r1"/><circle id="c1"/></svg>'
    )
    fake = FakeGdbus(export_payload=svg_text.encode())
    _install(monkeypatch, fake)
    transport = DBusTransport(settings)  # type: ignore[arg-type]
    ref = transport.get_active_document()
    assert ref.name == "poster.svg"
    assert ref.path is None
    assert ref.object_count == 2


def test_render_view_via_export_png(settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    png = b"\x89PNG\r\n\x1a\nFAKEPNG"
    fake = FakeGdbus(export_payload=png)
    _install(monkeypatch, fake)
    transport = DBusTransport(settings)  # type: ignore[arg-type]
    data = transport.render_view()
    assert data == png
    acts = _actions(fake)
    assert acts == ["export-filename", "export-type", "export-area-page", "export-do"]


def test_render_view_region_and_scale_map_to_actions(
    settings: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGdbus(export_payload=b"PNG")
    _install(monkeypatch, fake)
    transport = DBusTransport(settings)  # type: ignore[arg-type]
    transport.render_view(region=RenderRegion(x=10, y=20, width=30, height=40), scale=0.5)
    by_action = dict(fake.activations)
    assert "export-area" in by_action
    assert by_action["export-area"] == "[<'10.0:20.0:40.0:60.0'>]"
    assert "export-dpi" in by_action  # 96 * 0.5 = 48 dpi
    assert by_action["export-dpi"] == _variant_double(48.0)
    assert "export-area-page" not in by_action


def test_export_exceeding_input_cap_raises(
    settings: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGdbus(export_payload=b"x" * 64)
    _install(monkeypatch, fake)
    transport = DBusTransport(settings)  # type: ignore[arg-type]
    transport._settings = transport._settings.model_copy(update={"max_input_bytes": 8})
    with pytest.raises(LiveError):
        transport.get_document_svg()


# --- unsupported surface (stays on the socket backend) ---------------------


def test_selection_and_markup_methods_raise_unsupported(settings: object) -> None:
    transport = DBusTransport(settings)  # type: ignore[arg-type]
    with pytest.raises(LiveCapabilityUnsupported):
        transport.get_selection()
    with pytest.raises(LiveCapabilityUnsupported):
        transport.inspect_selection()
    with pytest.raises(LiveCapabilityUnsupported):
        transport.insert_svg("<rect/>")
    with pytest.raises(LiveCapabilityUnsupported):
        transport.set_selected_text("hi")


# --- viewport (window action group) ----------------------------------------


def test_set_viewport_fit_page(settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGdbus()
    _install(monkeypatch, fake)
    transport = DBusTransport(settings)  # type: ignore[arg-type]
    result = transport.set_viewport(mode="fit_page")
    assert result.applied is True
    assert "canvas-zoom-page" in _actions(fake)
    # Window-scoped: the activation targets the window object path, not the app path.
    assert any("window/1" in p for p in fake.object_paths)


def test_set_viewport_zoom_uses_absolute(settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGdbus()
    _install(monkeypatch, fake)
    transport = DBusTransport(settings)  # type: ignore[arg-type]
    transport.set_viewport(mode="zoom", zoom=2.0)
    by_action = dict(fake.activations)
    assert by_action["canvas-zoom-absolute"] == _variant_double(2.0)


def test_set_viewport_pan_unsupported(settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGdbus()
    _install(monkeypatch, fake)
    transport = DBusTransport(settings)  # type: ignore[arg-type]
    with pytest.raises(LiveCapabilityUnsupported):
        transport.set_viewport(mode="pan", dx=5.0, dy=5.0)


# --- apply-to-selection (style + transform) --------------------------------


def test_apply_to_selection_style(settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGdbus()
    _install(monkeypatch, fake)
    transport = DBusTransport(settings)  # type: ignore[arg-type]
    result = transport.apply_to_selection(style={"fill": "#00ff00"}, transform=None)
    assert result.undo_friendly is True
    assert result.affected_ids == []  # DBus cannot read selection ids
    by_action = dict(fake.activations)
    assert by_action["object-set-property"] == "[<'fill, #00ff00'>]"


def test_apply_to_selection_transform_decomposed(
    settings: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGdbus()
    _install(monkeypatch, fake)
    transport = DBusTransport(settings)  # type: ignore[arg-type]
    transport.apply_to_selection(style={}, transform="translate(15,5) rotate(10) scale(2)")
    by_action = dict(fake.activations)
    assert by_action["transform-translate"] == "[<'15,5'>]"
    assert by_action["transform-rotate"] == "[<'10'>]"
    assert by_action["transform-scale"] == "[<'2'>]"


def test_apply_to_selection_rejects_unknown_transform(
    settings: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGdbus()
    _install(monkeypatch, fake)
    transport = DBusTransport(settings)  # type: ignore[arg-type]
    with pytest.raises(LiveError):
        transport.apply_to_selection(style={}, transform="matrix(1,0,0,1,0,0)")


# --- GVariant injection guard (X1, sec.12) ---------------------------------


def test_variant_string_rejects_quote_injection() -> None:
    with pytest.raises(LiveError):
        _variant_string("'); evil")


def test_variant_string_rejects_backslash() -> None:
    with pytest.raises(LiveError):
        _variant_string("a\\b")


def test_variant_bool_and_double_format() -> None:
    assert _variant_bool(True) == "[<true>]"
    assert _variant_bool(False) == "[<false>]"
    assert _variant_double(2.0) == "[<2.0>]"


def test_variant_double_rejects_non_finite() -> None:
    with pytest.raises(LiveError):
        _variant_double(float("inf"))
    with pytest.raises(LiveError):
        _variant_double(float("nan"))


def test_get_document_svg_rejects_non_utf8(
    settings: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGdbus(export_payload=b"\xff\xfe not utf-8")
    _install(monkeypatch, fake)
    transport = DBusTransport(settings)  # type: ignore[arg-type]
    with pytest.raises(LiveError):
        transport.get_document_svg()


# --- live integration (self-skipping) --------------------------------------


def _live_instance_present() -> bool:
    if shutil.which("gdbus") is None or not dbus_backend._session_bus_present():
        return False
    return DBusTransport._actions_list_reachable(8.0)


@pytest.mark.skipif(
    not _live_instance_present(),
    reason="no running Inkscape on the session bus (no-freeze DBus path needs a live GUI instance)",
)
def test_live_export_no_freeze_roundtrip(settings: object) -> None:
    # Drives a genuine live instance: proves the export-to-file workaround returns real SVG over
    # DBus without freezing the GUI (the activation runs in Inkscape's own GLib main loop).
    transport = DBusTransport(settings)  # type: ignore[arg-type]
    transport.connect()
    try:
        svg = transport.get_document_svg()
        assert "<svg" in svg
        png = transport.render_view()
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        transport.disconnect()
