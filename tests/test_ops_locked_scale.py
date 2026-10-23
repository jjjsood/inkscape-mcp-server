"""Locked-layer change notes (pipeline-level) and scale neighbour notes / `about='selection'`.

Uses the brand_poster fixture: layer `layer-bg` is locked (holds `hero`, `band`), and `cta` is a
rect with a separate text `cta-label` on top of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from inkscape_mcp.edit.ops import OpError
from tests.conftest import run_ops
from tests.tool_support import call, open_doc, working_svg

FIXTURE = Path(__file__).parent / "e2e" / "fixtures" / "brand_poster.svg"
NS = "{http://www.w3.org/2000/svg}"
BLANK = b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300" viewBox="0 0 400 300"/>'


@pytest.fixture
def poster(tool_ws: Path) -> str:
    return open_doc(tool_ws, "poster.svg", FIXTURE.read_bytes())


def _transform(doc_id: str, oid: str) -> str | None:
    root = etree.fromstring(working_svg(doc_id).encode())
    return root.xpath(f"//*[@id='{oid}']")[0].get("transform")


def test_global_recolor_reports_locked_objects(poster: str) -> None:
    res = run_ops(poster, {"op": "replace_color", "from_color": "#e63946", "to_color": "#2a9d8f"})
    assert res.changed
    assert any("locked layer 'layer-bg'" in w and "band" in w for w in res.warnings)
    assert not any("cta" in w for w in res.warnings)  # unlocked objects are not listed


def test_locked_note_in_dry_run_and_other_ops(poster: str) -> None:
    dry = run_ops(
        poster,
        {"op": "replace_color", "from_color": "#e63946", "to_color": "#2a9d8f"},
        dry_run=True,
    )
    assert any("band" in w for w in dry.warnings)
    res = run_ops(poster, {"op": "move", "ids": ["hero"], "dx": 5, "dy": 0})
    assert any(
        "changed 1 object in locked layer 'layer-bg': hero" in w and "history(action='undo')" in w
        for w in res.warnings
    )


def test_grid_does_not_warn_about_its_own_locked_copies(tool_ws: Path, poster: str) -> None:
    sheet = open_doc(tool_ws, "sheet.svg", BLANK)
    res = run_ops(sheet, {"op": "grid", "doc_ids": [poster], "rows": 1, "cols": 1, "cell": 200})
    assert not any("locked layer" in w for w in res.warnings)


def test_no_locked_note_for_unlocked_edit(poster: str) -> None:
    res = run_ops(poster, {"op": "move", "ids": ["cta"], "dx": 5, "dy": 0})
    assert res.changed and not any("locked" in w for w in res.warnings)


def test_scale_flags_unscaled_label(poster: str) -> None:
    res = run_ops(poster, {"op": "scale", "ids": ["cta"], "sx": 1.2, "sy": 1.2})
    assert any(
        "cta-label sits on cta and was not scaled" in w and "about=" in w for w in res.warnings
    )
    assert _transform(poster, "cta-label") is None


def test_scale_about_selection_scales_together(poster: str) -> None:
    res = run_ops(
        poster, {"op": "scale", "ids": ["cta", "cta-label"], "sx": 2, "about": "selection"}
    )
    assert not any("not scaled" in w for w in res.warnings)
    # shared centre (94,198): matrix(2,0,0,2,-94,-198) on both
    assert (
        _transform(poster, "cta") == _transform(poster, "cta-label") == "matrix(2,0,0,2,-94,-198)"
    )


def test_scale_about_center_is_the_each_default(tool_ws: Path, poster: str) -> None:
    other = open_doc(tool_ws, "other.svg", FIXTURE.read_bytes())
    run_ops(poster, {"op": "scale", "ids": ["cta"], "sx": 2, "about": "center"})
    run_ops(other, {"op": "scale", "ids": ["cta"], "sx": 2})
    # about its own centre
    assert _transform(poster, "cta") == _transform(other, "cta") == "matrix(2,0,0,2,-94,-198)"


def test_selection_rejects_different_parents(poster: str) -> None:
    with pytest.raises(OpError, match="share one parent"):
        run_ops(poster, {"op": "scale", "ids": ["cta", "band"], "sx": 2, "about": "selection"})


def test_locked_layers_touched_is_structured(tool_app, tool_ws) -> None:
    doc = open_doc(tool_ws, "poster.svg", FIXTURE.read_bytes())
    recolor = [{"op": "replace_color", "from_color": "#e63946", "to_color": "#2a9d8f"}]
    plan = call(tool_app, "edit", {"doc_id": doc, "ops": recolor, "dry_run": True}).data
    assert plan["locked_layers_touched"] == {"layer-bg": ["band"]}
    done = call(tool_app, "edit", {"doc_id": doc, "ops": recolor}).data
    assert done["locked_layers_touched"] == {"layer-bg": ["band"]}
    move = [{"op": "move", "ids": ["cta"], "dx": 5}]
    moved = call(tool_app, "edit", {"doc_id": doc, "ops": move})
    assert "locked_layers_touched" not in moved.data
