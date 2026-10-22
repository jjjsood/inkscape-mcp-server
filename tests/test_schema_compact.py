"""`schema_compact`: boilerplate goes, validation semantics stay."""

from __future__ import annotations

import copy

import jsonschema

from inkscape_mcp.schema_compact import compact_schema

SCHEMA = {
    "title": "ToolArgs",
    "type": "object",
    "properties": {
        "doc_id": {"title": "Doc Id", "type": "string"},
        "limit": {
            "anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}],
            "default": None,
            "title": "Limit",
        },
        "mode": {"anyOf": [{"$ref": "#/$defs/Mode"}, {"type": "null"}], "default": None},
    },
    "required": ["doc_id"],
    "$defs": {"Mode": {"title": "Mode", "description": "class docstring", "enum": ["a", "b"]}},
}


def test_boilerplate_is_removed_and_nullable_folded() -> None:
    schema = copy.deepcopy(SCHEMA)
    compact_schema(schema)
    props = schema["properties"]
    assert "title" not in schema and "title" not in props["doc_id"]
    assert props["limit"] == {"type": ["integer", "null"], "minimum": 1}
    assert "default" not in props["mode"]
    assert "description" not in schema["$defs"]["Mode"]


def test_a_property_named_title_survives() -> None:
    schema = {"type": "object", "properties": {"title": {"type": "string"}}}
    compact_schema(schema)
    assert "title" in schema["properties"]


def test_accepted_and_rejected_arguments_are_unchanged() -> None:
    compacted = copy.deepcopy(SCHEMA)
    compact_schema(compacted)
    good = [{"doc_id": "d"}, {"doc_id": "d", "limit": 3}, {"doc_id": "d", "limit": None}]
    bad = [{}, {"doc_id": "d", "limit": 0}, {"doc_id": "d", "limit": "x"}, {"doc_id": 1}]
    for args in good:
        jsonschema.validate(args, SCHEMA)
        jsonschema.validate(args, compacted)
    for args in bad:
        for schema in (SCHEMA, compacted):
            try:
                jsonschema.validate(args, schema)
            except jsonschema.ValidationError:
                continue
            raise AssertionError(f"{args} accepted by {schema}")


def test_compaction_is_idempotent() -> None:
    once = copy.deepcopy(SCHEMA)
    compact_schema(once)
    twice = copy.deepcopy(once)
    compact_schema(twice)
    assert once == twice
