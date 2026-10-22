"""`help` beyond op docs: the capability matrix (engine + resource + topics), `stat_paths`, and
`goal` -> intent lookup as the CURRENT `intents.py` does it.

The intent map still names the pre-rework tools until P6 rewrites it to (tool, op) entries, so no
test here pins tool names or checks that the map references registered tools.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from inkscape_mcp.intents import INTENT_MAP, intents_summary, intents_summary_json, match_intents
from inkscape_mcp.runtime.capabilities import get_cached_capabilities, refresh_capabilities
from inkscape_mcp.runtime.probe import Capabilities
from inkscape_mcp.server import mcp, register_tools
from tests.tool_support import call

register_tools()


def _help(app, **args):
    return call(app, "help", args).data


def _registered() -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def _read(uri: str) -> str:
    async def go() -> str:
        async with Client(mcp) as client:
            return (await client.read_resource(uri))[0].text

    return asyncio.run(go())


# --- capabilities -------------------------------------------------------------------------------


def test_tool_and_resource_serve_the_same_cached_probe() -> None:
    refresh_capabilities()
    cached = get_cached_capabilities()
    from_resource = Capabilities.model_validate_json(_read("inkscape://runtime/capabilities"))
    assert cached == from_resource
    assert cached.probed_at == from_resource.probed_at  # one shared object, not two probes


def test_tool_count_is_the_registry_count() -> None:
    """One authoritative number: `tool_count` == `len(tools)` == the registered tools."""
    caps = get_cached_capabilities()
    assert caps.tool_count == len(caps.tools) == len(_registered())
    assert {t.name for t in caps.tools} == _registered()
    assert all(t.purpose for t in caps.tools)
    assert all(t.risk in {"low", "medium", "high", "restricted", "unknown"} for t in caps.tools)


def test_refresh_updates_the_cache_the_resource_serves() -> None:
    first = get_cached_capabilities()
    refreshed = refresh_capabilities()
    assert Capabilities.model_validate_json(_read("inkscape://runtime/capabilities")) == refreshed
    assert refreshed.inkscape_available == first.inkscape_available
    if not any("timed out" in n for n in (*first.notes, *refreshed.notes)):
        assert refreshed.actions == first.actions


def test_capabilities_and_diagnose_topics_differ_only_in_probing(tool_app) -> None:
    cached = _help(tool_app, topic="capabilities")["capabilities"]
    fresh = _help(tool_app, topic="diagnose")["capabilities"]
    for caps in (cached, fresh):
        assert "tools" not in caps and "tool_count" not in caps and "intents" not in caps
    assert fresh["inkscape_available"] == cached["inkscape_available"]
    # diagnose re-probes, so the cache now holds that probe
    assert get_cached_capabilities().probed_at == fresh["probed_at"]


# --- stat_paths ---------------------------------------------------------------------------------


def test_stat_matches_hashlib_and_the_filesystem(tool_app, tool_ws) -> None:
    files = {"a.png": b"aaaa" * 100, "sub/b.svg": b"<svg/>", "c.png": b"c"}
    (tool_ws / "sub").mkdir()
    for name, data in files.items():
        (tool_ws / name).write_bytes(data)
    # a relative and an absolute in-sandbox path both work; the echoed path is workspace-relative
    paths = ["a.png", str(tool_ws / "sub" / "b.svg"), "c.png"]

    stat = _help(tool_app, stat_paths=paths)["stat"]

    assert stat["total_bytes"] == sum(len(d) for d in files.values())
    by_path = {a["path"]: a for a in stat["artifacts"]}
    assert set(by_path) == set(files)
    for name, data in files.items():
        assert by_path[name]["bytes"] == (tool_ws / name).stat().st_size == len(data)
        assert by_path[name]["sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("how", ["absolute-outside", "traversal"])
def test_stat_rejects_paths_outside_the_workspace(tool_app, tool_ws, how: str) -> None:
    outside = tool_ws.parent / "outside.png"
    outside.write_bytes(b"secret")
    path = str(outside) if how == "absolute-outside" else "../outside.png"
    with pytest.raises(ToolError) as exc:
        _help(tool_app, stat_paths=[path])
    assert "path rejected" in str(exc.value) and str(tool_ws.parent) not in str(exc.value)


# --- goal -> intent lookup (current intents.py) -------------------------------------------------


def test_in_scope_goals_return_ranked_matches(tool_app) -> None:
    for goal in ("draw a rectangle", "export a png", "undo my last change / roll back"):
        matches = _help(tool_app, goal=goal)["goal"]["matches"]
        assert matches and all(m["use"] and m["how"] for m in matches)
        assert len(matches) <= 3
    top = _help(tool_app, goal="draw a rectangle")["goal"]["matches"][0]
    assert top["use"] == ["edit:create_rect"] and top["example"]["tool"] == "edit"


def test_matches_follow_the_engine_order_best_first(tool_app) -> None:
    goal = "export a png"
    expected = [m.keys for m in match_intents(goal, limit=3)]
    assert [m["use"] for m in _help(tool_app, goal=goal)["goal"]["matches"]] == expected


def test_match_limit_is_respected() -> None:
    assert len(match_intents("export png pdf icon batch web optimize", limit=2)) <= 2


@pytest.mark.parametrize(
    ("goal", "why_contains"),
    [
        ("edit a jpeg photo", ("raster", "pixel")),
        ("run an arbitrary extension", ("allow-listed", "`actions`")),
        ("download an image from a url", ("network", "url")),
        ("execute code on the host", ()),
    ],
)
def test_out_of_scope_goals_say_why_and_return_no_tool(tool_app, goal, why_contains) -> None:
    out = _help(tool_app, goal=goal)["goal"]
    assert out["out_of_scope"] is True and "matches" not in out
    if why_contains:
        assert any(word in out["why"].lower() or word in out["why"] for word in why_contains)


def test_inkscape_actions_are_guidance_not_a_refusal(tool_app) -> None:
    out = _help(tool_app, goal="run an arbitrary inkscape action")["goal"]
    assert "out_of_scope" not in out
    assert out["matches"][0]["use"][0].startswith("actions:")


def test_unmatched_goal_points_at_the_op_list(tool_app) -> None:
    out = _help(tool_app, goal="xyzzy plugh frobnicate quux")["goal"]
    assert "out_of_scope" not in out
    assert "help(topic='ops')" in out["matches"]


# --- the curated map: capabilities `intents` section + `inkscape://runtime/intents` -------------


def test_capabilities_carry_the_curated_intents() -> None:
    caps = get_cached_capabilities()
    assert len(caps.intents) == len(intents_summary()) == len(INTENT_MAP)
    entry = caps.intents[0]
    assert entry.goal_pattern and entry.targets and entry.how_to and entry.group
    restored = Capabilities.model_validate_json(caps.model_dump_json())
    assert [m.keys for m in restored.intents] == [m.keys for m in caps.intents]


def test_runtime_intents_resource_is_the_shared_accessor() -> None:
    async def listed() -> set[str]:
        async with Client(mcp) as client:
            return {str(r.uri) for r in await client.list_resources()}

    assert "inkscape://runtime/intents" in asyncio.run(listed())
    text = _read("inkscape://runtime/intents")
    assert text == intents_summary_json()
    from_resource = [
        (e["goal_pattern"], [f"{t['tool']}:{t['op']}" for t in e["targets"]], e["how_to"])
        for e in json.loads(text)["intents"]
    ]
    assert from_resource == [(m.goal_pattern, m.keys, m.how_to) for m in intents_summary()]
    caps = get_cached_capabilities()
    assert from_resource == [(m.goal_pattern, m.keys, m.how_to) for m in caps.intents]
