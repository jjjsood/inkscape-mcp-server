"""Authoring/compose prompt tests (E15-04, architecture §4.1).

The two E14 on-ramp prompts (`compose_artwork`, `restyle_artwork`) must be registered + discoverable
on the MCP prompt surface, weave in the caller's `goal`, reference only shipped E14 tools, and carry
no authority (they are pure guidance strings — no raw-Action / code path).
"""

from __future__ import annotations

import asyncio

from inkscape_mcp.prompts.authoring import compose_artwork, restyle_artwork
from inkscape_mcp.server import mcp

_EXPECTED = {"compose_artwork", "restyle_artwork"}


def test_authoring_prompts_registered() -> None:
    names = {p.name for p in asyncio.run(mcp.list_prompts())}
    assert _EXPECTED <= names


def test_compose_prompt_returns_non_empty_str_with_goal() -> None:
    body = compose_artwork("draw a blue logo")
    assert isinstance(body, str)
    assert body.strip()
    # The caller's goal is woven into the guidance.
    assert "draw a blue logo" in body


def test_compose_prompt_references_e14_create_render_export_tools() -> None:
    body = compose_artwork("anything")
    for tool in (
        "create_document",
        "create_rect",
        "create_text",
        "add_linear_gradient",
        "add_radial_gradient",
        "set_fill",
        "create_group",
        "find_objects",
        "render_preview",
        "validate_document",
        "export_document",
        "restore_snapshot",
    ):
        assert tool in body, f"compose_artwork omits {tool!r}"


def test_restyle_prompt_returns_non_empty_str_with_goal() -> None:
    body = restyle_artwork("make icons green")
    assert isinstance(body, str)
    assert body.strip()
    assert "make icons green" in body


def test_restyle_prompt_is_object_targeted_and_references_tools() -> None:
    body = restyle_artwork("anything")
    for tool in (
        "find_objects",
        "set_fill",
        "set_stroke",
        "render_preview",
        "export_document",
    ):
        assert tool in body, f"restyle_artwork omits {tool!r}"


def test_authoring_prompts_grant_no_raw_action_authority() -> None:
    # Guidance only — no raw-Action / code path is ever suggested (ADR-002/003).
    for body in (compose_artwork("x"), restyle_artwork("x")):
        assert "run_raw_action" not in body
        assert "--actions" not in body


def test_empty_goal_is_handled() -> None:
    # A blank goal degrades to a placeholder rather than rendering empty / breaking layout.
    for fn in (compose_artwork, restyle_artwork):
        body = fn("   ")
        assert "(no goal provided)" in body
