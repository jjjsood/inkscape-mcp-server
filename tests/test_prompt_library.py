"""Export/recolor prompt-library tests (architecture §4.1).

The four prompts must be registered + discoverable on the MCP prompt surface, reference only shipped
tools, and carry no authority (they are pure guidance strings).
"""

from __future__ import annotations

import asyncio

from inkscape_mcp.prompts.library import (
    prepare_icon_set,
    prepare_print_export,
    prepare_web_export,
    theme_recoloring,
)
from inkscape_mcp.server import mcp

_EXPECTED = {
    "prepare_web_export",
    "prepare_icon_set",
    "prepare_print_export",
    "theme_recoloring",
}


def test_all_four_prompts_registered() -> None:
    names = {p.name for p in asyncio.run(mcp.list_prompts())}
    assert _EXPECTED <= names


def test_web_export_prompt_points_at_shipped_tools() -> None:
    body = prepare_web_export()
    assert "export_web_profile" in body
    assert "export_batch" in body


def test_icon_set_prompt_points_at_shipped_tools() -> None:
    body = prepare_icon_set()
    assert "create_icon_set" in body


def test_print_export_prompt_points_at_shipped_tools() -> None:
    body = prepare_print_export()
    assert "export_print_profile" in body


def test_theme_recoloring_prompt_points_at_shipped_tools() -> None:
    body = theme_recoloring()
    assert "apply_palette" in body
    assert "replace_color" in body


def test_prompts_grant_no_raw_action_authority() -> None:
    # Guidance only — no raw-Action / code path is ever suggested.
    for body in (
        prepare_web_export(),
        prepare_icon_set(),
        prepare_print_export(),
        theme_recoloring(),
    ):
        assert "run_raw_action" not in body
        assert "--actions" not in body
