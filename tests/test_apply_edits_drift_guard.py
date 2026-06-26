"""`apply_edits` op-coverage drift-guard.

`apply_edits` is a discriminated union (:data:`inkscape_mcp.edit.batch.TypedEdit`) over the
typed single-document DOM ops. That union is hand-maintained: when a NEW pure-DOM single-doc edit
tool is added, nobody is forced to add the matching batch member, so `apply_edits` silently lags the
single-edit surface. This guard makes that divergence a RED test instead of a silent gap.

Definition of a SINGLE-DOCUMENT TYPED-DOM EDIT TOOL (the guard's scope), chosen to be precise:

- its defining MODULE leaf is one of the typed-DOM edit modules
  :data:`_EDIT_MODULE_LEAVES` (`create` / `style` / `text_object` / `transform` / `dom` / `compose`)
  — the modules whose tools mutate ONE document's DOM directly (ADR-005), and
- it is NOT read-only (`READ_ONLY_TOOLS`), and
- its risk class is `medium` or `high` (a real mutation, the batch's risk vocabulary).

By construction this EXCLUDES, without an allowlist entry:

- document lifecycle (`document` module: `create_document` / `open_document` / `reload_document`),
  save/optimize/snapshot/find/validate/discover/quality modules — not direct-DOM edits;
- the `paths` domain (`simplify_path`, `combine_paths`, the boolean/outline tools) — path GEOMETRY
  runs through the Inkscape ENGINE, not the direct-DOM kernel, so it is out of the batch surface by
  domain (deliberately NOT in `_NOT_BATCHABLE` — that would be a stale entry);
- read/render/export/live/system/actions tools — read-only or non-DOM.

So a NEW pure-DOM single-doc edit tool with no batch op gets flagged RED here, while
read/render/export/cross-doc-engine/path-engine tools are not falsely flagged.

Every flagged edit tool must be EITHER represented by an `op` literal in the `TypedEdit` union OR on
the curated :data:`_NOT_BATCHABLE` allowlist (each with a one-line rationale). The op set is
SINGLE-SOURCED from the union (introspected, never hand-copied), so adding/removing a member here
needs no edit to this test.

Both config flags are forced ON so the SYNC component registry holds the full superset (mirrors
`tests/test_classification_drift_guard.py`); the registry is read directly, so progressive
disclosure never hides a tool from this guard.
"""

from __future__ import annotations

import os
import typing

from fastmcp.tools import Tool

from inkscape_mcp.config import ENV_LIVE_ENABLED, ENV_RAW_ACTION_ENABLED, get_settings
from inkscape_mcp.edit.batch import TypedEdit
from inkscape_mcp.server import mcp, register_tools
from inkscape_mcp.tool_annotations import READ_ONLY_TOOLS
from inkscape_mcp.tool_tags import _module_leaf
from inkscape_mcp.tools.system import _risk_class

os.environ[ENV_LIVE_ENABLED] = "1"
os.environ[ENV_RAW_ACTION_ENABLED] = "1"
get_settings.cache_clear()
register_tools()


#: Defining-module leaves whose tools are typed-DOM edits of ONE document (ADR-005). A new such
#: module (or a new tool in one of these) is automatically in the guard's scope.
_EDIT_MODULE_LEAVES: frozenset[str] = frozenset(
    {"create", "style", "text_object", "transform", "dom", "compose"}
)

#: Curated allowlist: single-doc typed-DOM edit tools that are DELIBERATELY not `apply_edits`
#: members, each with a one-line rationale. Membership is a conscious "not batchable" choice, so
#: a NEW edit tool can never quietly skip both the union AND this list. Path-geometry tools
#: (`simplify_path` / `combine_paths` / boolean / outline) are NOT listed — they live in the
#: `paths` domain, outside `_EDIT_MODULE_LEAVES`, so they are excluded by domain, not by allowlist.
_NOT_BATCHABLE: dict[str, str] = {
    # cross-document composition — pull in / compose OTHER documents, breaking the one-doc /
    # one-snapshot atomic-batch contract.
    "compose_grid": "cross-document composition (lays out N source docs); multi-doc, not one-doc.",
    "place_document": "cross-document composition (places a source doc); multi-doc, not one-doc.",
    "insert_svg_fragment": (
        "adopts free-form external SVG under a parent; cross-document, high-risk allowlisted graft."
    ),
    "set_document_svg": (
        "replaces the WHOLE document from free-form SVG; whole-doc overwrite, not a typed DOM op."
    ),
    # engine op — needs the Inkscape engine to measure content bounds (`--query-all`).
    "fit_to_content": "engine op (`--query-all` content bounds); not a pure direct-DOM mutation.",
}


def _components() -> list[Tool]:
    """Every Tool in the sync registry — the full superset, pre-visibility-filter."""
    return [c for c in mcp._local_provider._components.values() if isinstance(c, Tool)]


def _edit_tool_names() -> set[str]:
    """Registered single-document typed-DOM edit tool names (the guard's scope; see module docs)."""
    return {
        c.name
        for c in _components()
        if _module_leaf(c) in _EDIT_MODULE_LEAVES
        and c.name not in READ_ONLY_TOOLS
        and _risk_class(c.description) in ("medium", "high")
    }


def _batchable_ops() -> set[str]:
    """The set of `op` discriminator literals in the `TypedEdit` union — single-sourced, not copied.

    `TypedEdit` is `Annotated[A | B | ... , Field(discriminator="op")]`; the first `get_args` peels
    the `Annotated` to the union, the second enumerates the member models, and each member's `op`
    field is a `Literal[...]` whose single argument is the op name.
    """
    (union, *_meta) = typing.get_args(TypedEdit)
    ops: set[str] = set()
    for member in typing.get_args(union):
        literal = member.model_fields["op"].annotation
        ops.update(typing.get_args(literal))
    return ops


def _uncovered_edit_tools(
    edit_tools: set[str], batchable: set[str], not_batchable: set[str]
) -> set[str]:
    """Edit tool names that are NEITHER an `apply_edits` op NOR on the not-batchable allowlist."""
    return edit_tools - batchable - not_batchable


# --- the op set is non-trivial + name-aligned --------------------------------


def test_typed_edit_ops_align_with_registered_tools() -> None:
    """Every `TypedEdit` `op` literal names a registered tool (no orphan/typo batch member)."""
    ops = _batchable_ops()
    assert ops, "expected a non-empty TypedEdit op surface"
    names = {c.name for c in _components()}
    orphan = ops - names
    assert not orphan, f"TypedEdit ops with no matching registered tool: {sorted(orphan)}"


# --- no stale allowlist entries ----------------------------------------------


def test_not_batchable_allowlist_has_no_stale_entries() -> None:
    """Every `_NOT_BATCHABLE` entry is an actually-flagged single-doc edit tool (no dead entry).

    Keeps the allowlist honest: if a tool is renamed/removed, or moves out of the edit-module scope
    (e.g. into the `paths` engine domain), its allowlist entry must be removed too.
    """
    edit_tools = _edit_tool_names()
    stale = set(_NOT_BATCHABLE) - edit_tools
    assert not stale, (
        f"_NOT_BATCHABLE references tool(s) not in the single-doc edit scope (remove them): "
        f"{sorted(stale)}"
    )


def test_not_batchable_and_batchable_are_disjoint() -> None:
    """A tool is a batch op XOR explicitly not-batchable — never both."""
    overlap = set(_NOT_BATCHABLE) & _batchable_ops()
    assert not overlap, f"tools in BOTH TypedEdit and _NOT_BATCHABLE: {sorted(overlap)}"


# --- the core parity guard ---------------------------------------------------


def test_every_edit_tool_is_batchable_or_explicitly_excluded() -> None:
    """Every single-doc typed-DOM edit tool is a `TypedEdit` op OR on `_NOT_BATCHABLE`."""
    edit_tools = _edit_tool_names()
    assert edit_tools, "expected a non-empty single-doc edit surface"
    uncovered = _uncovered_edit_tools(edit_tools, _batchable_ops(), set(_NOT_BATCHABLE))
    assert not uncovered, (
        "single-doc typed-DOM edit tools missing from BOTH the apply_edits TypedEdit union AND the "
        "_NOT_BATCHABLE allowlist — add an `op` member (reuse the existing engine builder) or list "
        f"it as not-batchable with a rationale: {sorted(uncovered)}"
    )


# --- guard goes RED for an unlisted batchable edit tool ----------------------


def test_guard_flags_an_unlisted_batchable_tool() -> None:
    """A synthetic edit tool in NEITHER the union NOR the allowlist is detected (proves RED).

    Mirrors `test_guard_flags_an_unclassified_high_risk_tool`: injects a name into the edit-tool set
    and asserts the parity check reports it, so a real new pure-DOM edit tool with no batch op would
    fail this guard rather than slip through.
    """
    fixture = "_fixture_unlisted_batchable_edit_tool"
    edit_tools = _edit_tool_names() | {fixture}
    uncovered = _uncovered_edit_tools(edit_tools, _batchable_ops(), set(_NOT_BATCHABLE))
    assert fixture in uncovered, (
        "guard must flag an edit tool that is neither batchable nor excluded"
    )
