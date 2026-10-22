"""`risk_for`: per-call risk from the arguments, the tool ceiling without them."""

from __future__ import annotations

import asyncio
import json

from inkscape_mcp.runtime.capabilities import risk_for
from inkscape_mcp.server import mcp, register_tools


def test_ceiling_without_arguments() -> None:
    assert risk_for("inspect") == "low"
    assert risk_for("edit") == "high"
    assert risk_for("open") == "medium"
    assert risk_for("nope") == "unknown"


def test_edit_risk_is_the_max_over_its_ops() -> None:
    style = {"op": "fill", "ids": ["a"], "color": "#fff"}
    delete = {"op": "delete", "ids": ["a"]}
    assert risk_for("edit", {"ops": [style]}) == "medium"
    assert risk_for("edit", {"ops": [style, delete]}) == "high"
    assert risk_for("open", {"ops": [delete]}) == "high"
    assert risk_for("open", {}) == "medium"


def test_unparseable_ops_fall_back_to_the_ceiling() -> None:
    assert risk_for("edit", {"ops": [{"op": "no_such_op"}]}) == "high"


def test_export_is_high_only_when_save_as_overwrites() -> None:
    plain = {"targets": [{"kind": "file", "format": "png"}]}
    save = {"targets": [{"kind": "save_as", "dest_path": "x.svg"}]}
    over = {"targets": [{"kind": "save_as", "dest_path": "x.svg", "overwrite": True}]}
    assert risk_for("export", plain) == "medium"
    assert risk_for("export", save) == "medium"
    assert risk_for("export", over) == "high"


def test_ops_resource_lists_every_op_with_an_example() -> None:
    register_tools()
    contents = asyncio.run(mcp.read_resource("inkscape://ops"))
    data = json.loads(contents.contents[0].content)
    assert "create_rect" in data["groups"]["create"]
    assert data["ops"]["delete"]["needs_approval"] is True
    assert data["ops"]["fill"]["example"]["op"] == "fill"
    assert all(entry["example"] for entry in data["ops"].values())
