"""`open(canvas=...)` / `open(svg=...)`: blank documents minted without a source file.

A canvas document returns the same `{doc_id, summary}` shape as an opened file, is seeded
byte-identically into `original.svg` and the working copy, and is `validate`-clean. Hermetic.
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from inkscape_mcp.registry import get_registry
from inkscape_mcp.workspace import sandbox
from tests.tool_support import call, working_svg


def _open_canvas(app, **canvas):
    return call(app, "open", {"canvas": canvas}).data


def _validate(app, doc_id: str) -> dict:
    return call(app, "inspect", {"doc_id": doc_id, "include": ["validate"]}).data["validate"]


def test_canvas_returns_open_shape_with_synthesized_viewbox(tool_app) -> None:
    data = _open_canvas(tool_app, width=120, height=80)
    assert data["doc_id"].startswith("d_")
    summary = data["summary"]
    assert (str(summary["width"]), str(summary["height"])) == ("120", "80")
    # No explicit viewBox -> `0 0 W H`, so a canvas document is never viewBox-less.
    assert summary["viewbox"] == [0, 0, 120, 80]
    assert "changed" not in data


def test_canvas_is_validate_clean(tool_app) -> None:
    doc_id = _open_canvas(tool_app, width=64, height=64)["doc_id"]
    report = _validate(tool_app, doc_id)
    assert report["ok"] is True


def test_canvas_explicit_viewbox(tool_app) -> None:
    data = _open_canvas(tool_app, width=100, height=100, viewBox="0 0 200 50")
    assert data["summary"]["viewbox"] == [0, 0, 200, 50]


def test_canvas_background_rect_stays_validate_clean(tool_app) -> None:
    doc_id = _open_canvas(tool_app, width=40, height=40, background="#3366cc")["doc_id"]
    svg = working_svg(doc_id)
    assert 'id="background"' in svg and "#3366cc" in svg
    assert _validate(tool_app, doc_id)["ok"] is True


def test_canvas_seeds_original_and_working_identically(tool_app, tool_ws) -> None:
    doc_id = _open_canvas(tool_app, width=10, height=10)["doc_id"]
    original = sandbox.original_copy(tool_ws, doc_id)
    working = sandbox.working_copy(tool_ws, doc_id)
    assert original.is_file() and working.is_file()
    assert original.read_bytes() == working.read_bytes()
    # No external source file: the conventional relative name is used.
    assert get_registry().get(doc_id).source_path == "document.svg"


@pytest.mark.parametrize(
    ("canvas", "match"),
    [
        ({"width": 0, "height": 50}, "greater than 0"),
        ({"width": 10, "height": 10, "viewBox": "not four numbers"}, "viewBox"),
        # a CSS-injection background is refused (normalize_color guards punctuation)
        ({"width": 10, "height": 10, "background": "red;stroke:url(http://evil)"}, None),
    ],
)
def test_canvas_rejects_bad_input(tool_app, canvas, match) -> None:
    with pytest.raises(ToolError, match=match):
        call(tool_app, "open", {"canvas": canvas})


def test_two_canvases_get_distinct_documents(tool_app) -> None:
    a = _open_canvas(tool_app, width=10, height=10)["doc_id"]
    b = _open_canvas(tool_app, width=10, height=10)["doc_id"]
    assert a != b
