"""The intent map is a guard-railed view of the tool surface: every `(tool, op)` it names exists and
every example call it shows validates against the real tool schema."""

from __future__ import annotations

import jsonschema
import pytest

from inkscape_mcp.edit.op_docs import op_models
from inkscape_mcp.edit.ops import parse_ops
from inkscape_mcp.intents import (
    INTENT_MAP,
    IntentEntry,
    _example_for,
    detect_out_of_scope,
    match_intents,
)
from tests.tool_support import target_ops, tool_schemas


def _label(entry: IntentEntry) -> str:
    return entry.goal_pattern


@pytest.fixture(scope="module")
def schemas() -> dict:
    from inkscape_mcp.tools import build_app

    return tool_schemas(build_app())


@pytest.mark.parametrize("entry", INTENT_MAP, ids=_label)
def test_targets_are_real_tools_and_ops(entry: IntentEntry, schemas: dict) -> None:
    assert entry.targets and entry.keywords and entry.how_to
    for target in entry.targets:
        assert target.tool in schemas, f"{entry.goal_pattern}: unknown tool {target.tool!r}"
        valid = target_ops(target.tool, schemas)
        if valid:
            assert target.op in valid, f"{target.key}: op not one of {sorted(valid)}"
        else:
            assert target.op is None, f"{target.key}: {target.tool} has no op values"


@pytest.mark.parametrize("entry", INTENT_MAP, ids=_label)
def test_example_call_validates_against_the_tool_schema(entry: IntentEntry, schemas: dict) -> None:
    example = _example_for(entry)
    assert example is not None, f"{entry.goal_pattern}: no example call"
    assert example.tool in {t.tool for t in entry.targets}
    jsonschema.validate(example.args, schemas[example.tool])
    if example.tool in ("edit", "open") and "ops" in example.args:
        parse_ops(example.args["ops"])  # the ops themselves are typed, not just `list[dict]`


@pytest.mark.parametrize("entry", INTENT_MAP, ids=_label)
def test_the_example_demonstrates_the_first_target(entry: IntentEntry) -> None:
    example = _example_for(entry)
    first = entry.targets[0]
    assert example is not None and example.tool == first.tool
    if first.tool == "edit":
        assert first.op in {o["op"] for o in example.args.get("ops", [])}


def test_every_common_edit_op_is_reachable_from_some_goal() -> None:
    reachable = {t.op for e in INTENT_MAP for t in e.targets if t.tool == "edit"}
    unreachable = set(op_models()) - reachable
    assert not unreachable, f"no intent routes to these ops: {sorted(unreachable)}"


def test_map_has_unique_goals() -> None:
    goals = [e.goal_pattern for e in INTENT_MAP]
    assert len(goals) == len(set(goals))


def test_actions_are_discoverable_not_out_of_scope() -> None:
    assert detect_out_of_scope("run an inkscape action chain") is None
    (top, *_) = match_intents("run an inkscape action chain")
    assert top.keys[0].startswith("actions:")
    assert detect_out_of_scope("run an arbitrary extension") is not None


def test_out_of_scope_raster_text_does_not_overclaim() -> None:
    hit = detect_out_of_scope("edit my jpeg")
    assert hit is not None
    assert "trace a raster" not in hit.reason and "can embed" not in hit.reason


def test_german_goals_match_and_every_german_key_names_a_real_intent() -> None:
    from inkscape_mcp.intents import _GERMAN_KEYWORDS

    assert set(_GERMAN_KEYWORDS) <= {e.goal_pattern for e in INTENT_MAP}
    for goal, key in [
        ("Wie mache ich ein SVG kleiner fürs Web?", "edit:optimize_web"),
        ("Funktioniert meine Inkscape-Installation?", "help:diagnose"),
        ("Mach das rückgängig", "history:undo"),
        ("Exportiere das als PNG", "export:file"),
    ]:
        assert key in match_intents(goal, limit=1)[0].keys, goal


@pytest.mark.parametrize(
    "goal",
    [
        "clean up the history",
        "tidy history",
        "free snapshot space",
        "reduce history",
        "prune old snapshots",
    ],
)
def test_history_cleanup_goals_surface_prune(goal: str) -> None:
    (top, *_) = match_intents(goal)
    assert "history:prune" in top.keys
    assert top.example is not None and top.example.args["action"] == "prune"


def test_open_examples_do_not_add_a_background() -> None:
    from inkscape_mcp.tools import open_

    assert "'background'" not in open_.DESCRIPTION.split("Example:")[1]
