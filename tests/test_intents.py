"""The intent map is a guard-railed view of the tool surface: every `(tool, op)` it names exists and
every example call it shows validates against the real tool schema."""

from __future__ import annotations

import json

import jsonschema
import pytest

from inkscape_mcp.edit.op_docs import OP_EXAMPLES, OP_GROUPS, op_models
from inkscape_mcp.edit.ops import parse_ops
from inkscape_mcp.intents import (
    INTENT_MAP,
    IntentEntry,
    _example_for,
    detect_out_of_scope,
    intents_summary,
    intents_summary_json,
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
        valid = target_ops(target.tool, schemas[target.tool])
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
    assert example is not None
    if first.tool == "edit":
        used = {o["op"] for o in example.args.get("ops", [])}
        assert first.op in used or example.tool != "edit"


def test_every_op_has_an_example_and_a_group() -> None:
    ops = set(op_models())
    assert ops == set(OP_EXAMPLES), "an op without a validated example (or a stale one)"
    assert ops == {op for group in OP_GROUPS.values() for op in group}, "an op in no group"


def test_every_common_edit_op_is_reachable_from_some_goal() -> None:
    reachable = {t.op for e in INTENT_MAP for t in e.targets if t.tool == "edit"}
    unreachable = set(op_models()) - reachable
    assert not unreachable, f"no intent routes to these ops: {sorted(unreachable)}"


def test_map_has_unique_goals() -> None:
    goals = [e.goal_pattern for e in INTENT_MAP]
    assert len(goals) == len(set(goals))


def test_summary_and_resource_share_the_map() -> None:
    summary = intents_summary()
    assert [m.goal_pattern for m in summary] == [e.goal_pattern for e in INTENT_MAP]
    body = json.loads(intents_summary_json())
    assert len(body["intents"]) == len(INTENT_MAP)
    first = body["intents"][0]
    assert first["targets"][0] == {"tool": "edit", "op": "create_rect"}
    assert first["example"]["tool"] == "edit" and "keywords" not in first


def test_match_returns_keys_and_example() -> None:
    (top, *_) = match_intents("draw a rectangle")
    assert top.keys == ["edit:create_rect"]
    assert top.example is not None and top.example.args["ops"][0]["op"] == "create_rect"


def test_actions_are_discoverable_not_out_of_scope() -> None:
    assert detect_out_of_scope("run an inkscape action chain") is None
    (top, *_) = match_intents("run an inkscape action chain")
    assert top.keys[0].startswith("actions:")
    assert detect_out_of_scope("run an arbitrary extension") is not None
