"""Shrink the per-tool input schemas FastMCP generates, in place (`tool_tags.apply_tool_meta`).

`tools/list` is resent every turn, so schema boilerplate costs tokens on every call. This pass
removes what carries no information for a model:

- `title` on schema nodes (FastMCP/pydantic derive it from the property name),
- `default: null` (an optional parameter is already absent from `required`),
- the class docstring pydantic copies into `$defs` entries,
- `anyOf: [X, {type: null}]` is folded into `type: [X, "null"]` when `X` is a plain typed node.

Semantics are unchanged: every accepted argument set stays valid and every rejected one stays
rejected. The pass is idempotent.
"""

from __future__ import annotations

from typing import Any

_NULL = {"type": "null"}
_SCHEMA_MAPS = ("properties", "$defs", "definitions", "patternProperties")
_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_NODES = ("items", "additionalProperties", "not", "if", "then", "else")


def _fold_nullable(node: dict[str, Any]) -> None:
    members = node.get("anyOf")
    if not isinstance(members, list) or len(members) != 2 or _NULL not in members:
        return
    (other,) = (m for m in members if m != _NULL)
    kind = other.get("type") if isinstance(other, dict) else None
    if not isinstance(kind, str):
        return
    del node["anyOf"]
    node.update({k: v for k, v in other.items() if k != "type"})
    node["type"] = [kind, "null"]


def compact_schema(node: Any, *, is_def: bool = False) -> None:
    """Compact one JSON-schema node (and everything below it) in place."""
    if not isinstance(node, dict):
        return
    node.pop("title", None)
    if "default" in node and node["default"] is None:
        del node["default"]
    if is_def:
        node.pop("description", None)
    for key in _SCHEMA_MAPS:
        children = node.get(key)
        if isinstance(children, dict):
            for child in children.values():
                compact_schema(child, is_def=key in ("$defs", "definitions"))
    for key in _SCHEMA_LISTS:
        children = node.get(key)
        if isinstance(children, list):
            for child in children:
                compact_schema(child)
    for key in _SCHEMA_NODES:
        compact_schema(node.get(key))
    _fold_nullable(node)
