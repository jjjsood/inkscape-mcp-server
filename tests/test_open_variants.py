"""`open`: every source, the exactly-one rule, path sandboxing, and the open + first-ops flow."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from tests.tool_support import SVG, call, working_svg, write_svg


def test_open_path(tool_app, tool_ws) -> None:
    write_svg(tool_ws)
    res = call(tool_app, "open", {"path": "logo.svg"}).data
    assert res["doc_id"].startswith("d_")
    assert res["summary"]["objects"] >= 3
    assert "working copy" in res["persist_hint"] and "save_as" in res["persist_hint"]
    assert "changed" not in res


def test_open_svg_text_is_scrubbed_and_registered(tool_app) -> None:
    svg = SVG.decode().replace("<text", "<script>alert(1)</script><text")
    # a script element is outside the allowlist: the call must fail rather than store it
    with pytest.raises(ToolError):
        call(tool_app, "open", {"svg": svg})
    res = call(tool_app, "open", {"svg": SVG.decode()}).data
    assert "<rect" in working_svg(res["doc_id"])


def test_open_svg_requires_svg_root(tool_app) -> None:
    with pytest.raises(ToolError):
        call(tool_app, "open", {"svg": "<g xmlns='http://www.w3.org/2000/svg'/>"})


def test_open_requires_exactly_one_source(tool_app, tool_ws) -> None:
    write_svg(tool_ws)
    with pytest.raises(ToolError, match="exactly one source"):
        call(tool_app, "open", {})
    with pytest.raises(ToolError, match="exactly one source"):
        call(tool_app, "open", {"path": "logo.svg", "canvas": {"width": 1, "height": 1}})


def test_open_with_ops_returns_created_ids_in_one_call(tool_app, tool_renders) -> None:
    res = call(
        tool_app,
        "open",
        {
            "canvas": {"width": 200, "height": 100},
            "ops": [
                {"op": "create_rect", "x": 0, "y": 0, "width": 50, "height": 50, "id": "bg"},
                {"op": "fill", "ids": ["$0"], "color": "#336699"},
            ],
            "render": "inline",
        },
    )
    data = res.data
    assert data["changed"] is True
    assert data["created"][0]["id"] == "bg"
    assert "#336699" in working_svg(data["doc_id"])
    assert len(res.images) == 1
    assert tool_renders == [data["doc_id"]]


def test_open_with_failing_ops_keeps_the_document_and_names_it(tool_app) -> None:
    with pytest.raises(
        ToolError, match=r"opened (\S+), but ops failed: ops\[0\] fill.*call edit\(doc_id='d_"
    ):
        call(
            tool_app,
            "open",
            {
                "canvas": {"width": 10, "height": 10},
                "ops": [{"op": "fill", "ids": ["nope"], "color": "#fff"}],
            },
        )


def test_render_inline_without_ops_shows_the_opened_page(tool_app, tool_renders) -> None:
    res = call(tool_app, "open", {"canvas": {"width": 10, "height": 10}, "render": "inline"})
    assert len(res.images) == 1 and tool_renders == [res.data["doc_id"]]
    assert "changed" not in res.data


# --- path handling -----------------------------------------------------------------------


def test_open_relative_path_anchors_to_the_workspace_root(
    tool_app, tool_ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolved against the first root, NOT the process CWD."""
    (tool_ws / "e2e").mkdir()
    (tool_ws / "e2e" / "brandmark.svg").write_bytes(SVG)
    elsewhere = tool_ws.parent / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    data = call(tool_app, "open", {"path": "e2e/brandmark.svg"}).data
    assert data["doc_id"].startswith("d_")


def _outside(tool_ws, how: str) -> str:
    outside = tool_ws.parent / "outside.svg"
    outside.write_bytes(SVG)
    if how == "absolute":
        return str(outside)
    if how == "traversal":
        return "../outside.svg"
    (tool_ws / "link.svg").symlink_to(outside)
    return "link.svg"


@pytest.mark.parametrize("how", ["absolute", "traversal", "symlink"])
def test_open_outside_the_workspace_is_rejected_without_a_host_path(
    tool_app, tool_ws, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    monkeypatch.chdir(tool_ws)
    path = _outside(tool_ws, how)
    with pytest.raises(ToolError) as exc:
        call(tool_app, "open", {"path": path})
    assert "path rejected: outside workspace" in str(exc.value)
    assert str(tool_ws.parent) not in str(exc.value)
