"""Intent → tool discoverability tests.

Covers the `how_do_i` tool, the curated intent map, the out-of-scope detector, and the additive
`intents` section on `list_capabilities`. The load-bearing test is `test_intent_map_*_real_tools`:
it asserts every tool name in the curated map (and in the capabilities `intents` section) is an
actually-registered tool, so the map cannot silently drift from the live tool surface.
"""

from __future__ import annotations

import asyncio
import json

from fastmcp import Client

from inkscape_mcp.intents import (
    INTENT_MAP,
    intents_summary,
    intents_summary_json,
    match_intents,
)
from inkscape_mcp.runtime.probe import Capabilities
from inkscape_mcp.server import mcp, register_tools
from inkscape_mcp.tools.discover import HowDoIResult, how_do_i
from inkscape_mcp.tools.system import list_capabilities

# Register the full surface once so `mcp.list_tools()` reflects every tool (idempotent).
register_tools()


def _tool_names() -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


# --- tool registration -----------------------------------------------------------------------


def test_how_do_i_registered() -> None:
    assert "how_do_i" in _tool_names()


def test_how_do_i_returns_result_model() -> None:
    assert isinstance(how_do_i("draw a rectangle"), HowDoIResult)


# --- in-scope matching: goal -> correct real tool --------------------------------------------


def test_draw_rectangle_maps_to_create_rect() -> None:
    res = how_do_i("draw a rectangle")
    assert res.out_of_scope is False
    assert res.matches
    assert "create_rect" in res.matches[0].tools


def test_make_svg_smaller_maps_to_web_optimize() -> None:
    res = how_do_i("make my svg smaller for web")
    assert res.out_of_scope is False
    assert any("svg_web_optimize" in m.tools for m in res.matches)


def test_find_red_shapes_maps_to_find_objects() -> None:
    res = how_do_i("find the red shapes")
    assert res.out_of_scope is False
    assert any("find_objects" in m.tools for m in res.matches)


def test_export_png_maps_to_export_or_render() -> None:
    res = how_do_i("export a png")
    assert res.out_of_scope is False
    tools = {t for m in res.matches for t in m.tools}
    assert tools & {"export_document", "render_preview"}


def test_change_fill_color_maps_to_set_fill() -> None:
    res = how_do_i("change the fill color of a shape")
    assert res.out_of_scope is False
    assert any("set_fill" in m.tools for m in res.matches)


def test_undo_maps_to_snapshot_tools() -> None:
    res = how_do_i("undo my last change / roll back")
    assert res.out_of_scope is False
    tools = {t for m in res.matches for t in m.tools}
    assert "restore_snapshot" in tools


def test_matches_are_ranked_best_first() -> None:
    """A precise goal puts its dedicated entry first (more keyword hits = higher score)."""
    res = how_do_i("add text label")
    assert res.matches
    assert "create_text" in res.matches[0].tools


# --- out-of-scope detection ------------------------------------------------------------------


def test_edit_jpeg_photo_is_out_of_scope() -> None:
    res = how_do_i("edit a jpeg photo")
    assert res.out_of_scope is True
    assert res.matches == []
    assert res.note  # names why (vector-only)
    assert "raster" in res.note.lower() or "pixel" in res.note.lower()


def test_run_arbitrary_extension_is_out_of_scope() -> None:
    res = how_do_i("run an arbitrary extension")
    assert res.out_of_scope is True
    assert res.matches == []
    assert "ADR-003" in res.note or "escape hatch" in res.note.lower()


def test_download_image_from_url_is_out_of_scope() -> None:
    res = how_do_i("download an image from a url")
    assert res.out_of_scope is True
    assert res.matches == []
    assert "network" in res.note.lower() or "url" in res.note.lower()


def test_execute_code_is_out_of_scope() -> None:
    res = how_do_i("execute code on the host")
    assert res.out_of_scope is True
    assert res.matches == []


def test_out_of_scope_beats_incidental_match() -> None:
    """Out-of-scope is checked first: 'run an arbitrary action' must not return a tool."""
    res = how_do_i("run an arbitrary inkscape action")
    assert res.out_of_scope is True
    assert res.matches == []


# --- no-match -------------------------------------------------------------------------------


def test_no_match_returns_empty_with_suggestion() -> None:
    res = how_do_i("xyzzy plugh frobnicate quux")
    assert res.out_of_scope is False
    assert res.matches == []
    assert "list_capabilities" in res.note


# --- capabilities `intents` section ----------------------------------------------------------


def test_list_capabilities_includes_intents() -> None:
    caps = list_capabilities()
    assert isinstance(caps, Capabilities)
    assert caps.intents  # non-empty curated map
    # Each intents entry carries the guidance shape.
    entry = caps.intents[0]
    assert entry.goal_pattern
    assert entry.tools
    assert entry.how_to
    assert entry.group


def test_capabilities_intents_round_trip() -> None:
    """Additive `intents` field survives JSON round-trip (resource serialization safety)."""
    caps = list_capabilities()
    restored = Capabilities.model_validate_json(caps.model_dump_json())
    assert [m.tools for m in restored.intents] == [m.tools for m in caps.intents]


def test_intents_summary_matches_map_size() -> None:
    assert len(intents_summary()) == len(INTENT_MAP)


# --- load-bearing guard: the map references only REAL registered tools ------------------------


def test_intent_map_references_only_real_tools() -> None:
    """Every tool named anywhere in the curated INTENT_MAP must be a registered tool.

    Guards against the map drifting from the actual tool surface (e.g. a renamed/removed tool).
    """
    registered = _tool_names()
    referenced = {tool for entry in INTENT_MAP for tool in entry.tools}
    missing = referenced - registered
    assert not missing, f"intent map references non-registered tools: {sorted(missing)}"


def test_capabilities_intents_reference_only_real_tools() -> None:
    """The same guarantee for the `intents` section served by list_capabilities."""
    registered = _tool_names()
    caps = list_capabilities()
    referenced = {tool for entry in caps.intents for tool in entry.tools}
    missing = referenced - registered
    assert not missing, f"capabilities intents reference non-registered tools: {sorted(missing)}"


def test_match_intents_limit_is_respected() -> None:
    assert len(match_intents("export png pdf icon batch web optimize", limit=2)) <= 2


# ---: inkscape://runtime/intents resource (no-drift parity) ---------------------------


def _read_runtime_intents() -> str:
    """Read the `inkscape://runtime/intents` resource over the in-memory client; return its text."""

    async def _run() -> str:
        async with Client(mcp) as client:
            res = await client.read_resource("inkscape://runtime/intents")
        return res[0].text

    return asyncio.run(_run())


def test_runtime_intents_resource_is_registered() -> None:
    """The curated-intents resource is a concrete (non-templated) resource the client can list."""

    async def _run() -> set[str]:
        async with Client(mcp) as client:
            resources = await client.list_resources()
        return {str(r.uri) for r in resources}

    assert "inkscape://runtime/intents" in asyncio.run(_run())


def test_runtime_intents_resource_matches_shared_accessor() -> None:
    """The resource payload IS the shared `intents_summary_json` accessor output (byte-for-byte)."""
    assert _read_runtime_intents() == intents_summary_json()


def test_runtime_intents_resource_matches_how_do_i_map() -> None:
    """No drift: the resource exposes the SAME goal→tool entries as the in-memory curated map.

    Parity is asserted against `intents_summary()` — the single source `how_do_i`,
    `list_capabilities`, and this resource all derive from — so they cannot diverge.
    """
    payload = json.loads(_read_runtime_intents())
    from_resource = [
        (e["goal_pattern"], e["tools"], e["how_to"], e["group"]) for e in payload["intents"]
    ]
    from_map = [(m.goal_pattern, m.tools, m.how_to, m.group) for m in intents_summary()]
    assert from_resource == from_map
    # And the same map the capabilities `intents` section serves (no per-surface drift).
    caps_map = [(m.goal_pattern, m.tools, m.how_to, m.group) for m in list_capabilities().intents]
    assert from_resource == caps_map


def test_runtime_intents_resource_references_only_real_tools() -> None:
    """Every tool named in the resource payload must be a registered tool (no dangling refs)."""
    registered = _tool_names()
    payload = json.loads(_read_runtime_intents())
    referenced = {tool for entry in payload["intents"] for tool in entry["tools"]}
    missing = referenced - registered
    assert not missing, (
        f"runtime/intents resource references non-registered tools: {sorted(missing)}"
    )
