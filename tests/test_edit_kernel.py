"""Op-kernel tests (`edit/ops.py`): typed ops, `$n` refs, selectors, atomicity, risk, dry run.

The guarantees the kernel owes a caller:

1. every op is validated; a bad op leaves the document BYTE-IDENTICAL (no snapshot, no applied
   record) and the error names the op (`ops[i] <op>: …`);
2. ONE snapshot + ONE Operation Record per call, and one `restore_snapshot` reverts all of it;
3. effective risk = MAX over the ops (a `delete` forces the `approval_token` gate);
4. `$<n>` names an id an earlier op of the same call created, and a selector sees objects created
   earlier in the same call;
5. path ops are refused when raw actions are disabled (engine behaviour: test_ops_paths.py);
6. `dry_run` returns the plan and writes nothing; `render=True` renders exactly once.

Hermetic: both Inkscape touch points (`render_preview`, the path engine's `run_inkscape`) are faked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from lxml import etree

from inkscape_mcp.config import (
    ENV_EDIT_PREVIEWS,
    ENV_RAW_ACTION_ENABLED,
    ENV_WORKSPACE_ROOTS,
    get_settings,
)
from inkscape_mcp.edit import ops as ops_mod
from inkscape_mcp.edit import paths as paths_engine
from inkscape_mcp.edit.ops import MAX_OPS, OpError, parse_ops
from inkscape_mcp.edit.pipeline import EditApplyError
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render.cli import RenderResult
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.subprocess_exec import ProcessResult
from tests.conftest import run_ops

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
    b'<rect id="r1" x="0" y="0" width="10" height="10" fill="#ff0000"/>'
    b'<rect id="r2" x="20" y="0" width="10" height="10" fill="#ff0000"/>'
    b'<text id="t1" x="0" y="20">hello</text>'
    b"</svg>"
)

TOKEN = "approved-by-operator"

#: What the fake path engine returns: a single merged path that keeps the bottom id `pa`.
ENGINE_OUTPUT = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
    b'<path id="pa" d="M0,0 L10,0 L10,10 Z"/>'
    b"</svg>"
)

#: Render calls seen by the pipeline's and the kernel's `render_preview` fakes.
_PIPELINE_RENDERS: list[str] = []
_KERNEL_RENDERS: list[str] = []


def _fake_render(calls: list[str]) -> Any:
    def render(doc_id: str, width_px: int | None = None, **_: object) -> RenderResult:
        calls.append(doc_id)
        root = Path(get_registry().get(doc_id).root)
        preview_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        out = preview_dir / "preview-auto.png"
        out.write_bytes(b"\x89PNG\r\n\x1a\n")
        rel = out.relative_to(root).as_posix()
        return RenderResult(
            doc_id=doc_id,
            workspace_relative_path=rel,
            format="png",
            width_px=10,
            height_px=10,
            duration_s=0.0,
        )

    return render


@pytest.fixture
def doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    _PIPELINE_RENDERS.clear()
    _KERNEL_RENDERS.clear()
    monkeypatch.setattr(
        "inkscape_mcp.edit.pipeline.render_preview", _fake_render(_PIPELINE_RENDERS)
    )
    monkeypatch.setattr(ops_mod, "render_preview", _fake_render(_KERNEL_RENDERS))

    def fake_run_inkscape(args: list[str], settings: object | None = None) -> ProcessResult:
        out = next(a[len("--export-filename=") :] for a in args if a.startswith("--export-file"))
        Path(out).write_bytes(ENGINE_OUTPUT)
        return ProcessResult(
            args=list(args), returncode=0, stdout="", stderr="", duration_s=0.0, timed_out=False
        )

    monkeypatch.setattr(paths_engine, "run_inkscape", fake_run_inkscape)
    src = ws / "logo.svg"
    src.write_bytes(SVG)
    return get_registry().open_document(str(src)).doc_id, ws


def _working(root: Path, doc_id: str) -> etree._Element:
    return etree.fromstring(sandbox.working_copy(root, doc_id).read_bytes())


def _find(root: etree._Element, object_id: str) -> etree._Element:
    for elem in root.iter():
        if isinstance(elem.tag, str) and elem.get("id") == object_id:
            return elem
    raise AssertionError(f"no element with id {object_id!r}")


def _has(root: etree._Element, object_id: str) -> bool:
    return any(isinstance(e.tag, str) and e.get("id") == object_id for e in root.iter())


def _style(elem: etree._Element) -> dict[str, str]:
    decls: dict[str, str] = {}
    for part in (elem.get("style") or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            decls[k.strip()] = v.strip()
    return decls


def _snapshot_count(root: Path, doc_id: str) -> int:
    index = sandbox.snapshots_index(root, doc_id)
    return len(json.loads(index.read_text())["snapshots"]) if index.is_file() else 0


def _records(root: Path, doc_id: str) -> list[dict[str, Any]]:
    d = sandbox.operations_dir(root, doc_id)
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.is_dir() else []


# --- one operation per call -------------------------------------------------


def test_ops_apply_under_one_snapshot_and_record(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    result = run_ops(
        doc_id,
        {"op": "fill", "ids": ["r1"], "color": "#0000ff"},
        {"op": "move", "ids": ["r1"], "dx": 5, "dy": 0},
        {"op": "create_rect", "x": 50, "y": 50, "width": 20, "height": 20, "id": "bg"},
    )

    assert result.changed is True
    assert result.risk == "medium"
    working = _working(root, doc_id)
    assert _style(_find(working, "r1"))["fill"] == "#0000ff"
    assert _find(working, "r1").get("transform") == "translate(5,0)"
    assert _has(working, "bg")

    (record,) = _records(root, doc_id)
    assert record["tool"] == "edit"
    assert record["status"] == "applied"
    assert record["snapshot_id"] == result.snapshot_id
    assert _snapshot_count(root, doc_id) == 1
    # No Inkscape render per edit by default.
    assert _PIPELINE_RENDERS == [] and _KERNEL_RENDERS == []


def test_created_reports_id_and_bbox(doc: tuple[str, Path]) -> None:
    doc_id, _ = doc
    result = run_ops(
        doc_id,
        {"op": "fill", "ids": ["r1"], "color": "#00ff00"},
        {"op": "create_rect", "x": 50, "y": 60, "width": 20, "height": 30, "id": "bg"},
        {"op": "create_circle", "cx": 5, "cy": 5, "r": 2},
    )
    assert [c.op for c in result.created] == [1, 2]
    assert result.created[0].id == "bg"
    assert result.created[0].bbox == [50.0, 60.0, 20.0, 30.0]
    assert result.created[1].id.startswith("circle-")
    assert [o.name for o in result.outcomes] == ["fill", "create_rect", "create_circle"]


def test_restore_snapshot_reverts_whole_call(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    result = run_ops(
        doc_id,
        {"op": "fill", "ids": ["r1"], "color": "#0000ff"},
        {"op": "create_circle", "cx": 5, "cy": 5, "r": 3, "id": "c1"},
    )
    assert sandbox.working_copy(root, doc_id).read_bytes() != before
    restore_snapshot(doc_id, result.snapshot_id)
    assert sandbox.working_copy(root, doc_id).read_bytes() == before


# --- atomicity / rollback ---------------------------------------------------


def _assert_untouched(root: Path, doc_id: str, before: bytes) -> None:
    assert sandbox.working_copy(root, doc_id).read_bytes() == before
    assert _snapshot_count(root, doc_id) == 0
    # A failed call may leave a record, but only a DISCARDED one — never proposed/applied.
    assert {r["status"] for r in _records(root, doc_id)} <= {"discarded"}


def test_invalid_value_rolls_back_everything(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    with pytest.raises(OpError, match=r"ops\[1\] fill: "):
        run_ops(
            doc_id,
            {"op": "fill", "ids": ["r1"], "color": "#00ff00"},
            {"op": "fill", "ids": ["r1"], "color": "notacolor;}"},
        )
    _assert_untouched(root, doc_id, before)


def test_unknown_id_without_a_close_match_lists_the_ids_of_a_small_document(
    doc: tuple[str, Path],
) -> None:
    doc_id, _root = doc
    with pytest.raises(OpError, match=r"id 'logo2' not found \(ids in document: .*r1"):
        run_ops(doc_id, {"op": "fill", "ids": ["logo2"], "color": "#123456"})


def test_unknown_id_is_actionable_and_rolls_back(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    with pytest.raises(OpError, match=r"ops\[1\] fill: id 'r11' not found \(similar: r1"):
        run_ops(
            doc_id,
            {"op": "fill", "ids": ["r1"], "color": "#123456"},
            {"op": "fill", "ids": ["r11"], "color": "#123456"},
        )
    _assert_untouched(root, doc_id, before)


def test_unknown_document(doc: tuple[str, Path]) -> None:
    with pytest.raises(EditApplyError, match="document id not found"):
        run_ops("nope", {"op": "normalize_viewbox"})


# --- list-level validation ----------------------------------------------------


def test_empty_and_oversized_lists_are_rejected(doc: tuple[str, Path]) -> None:
    doc_id, _ = doc
    with pytest.raises(OpError, match="at least one"):
        run_ops(doc_id)
    too_many = [{"op": "normalize_viewbox"}] * (MAX_OPS + 1)
    with pytest.raises(OpError, match=f"at most {MAX_OPS}"):
        run_ops(doc_id, *too_many)


def test_schema_errors_name_the_first_three_problems(doc: tuple[str, Path]) -> None:
    with pytest.raises(OpError) as exc:
        parse_ops(
            [
                {"op": "create_rect", "x": "a", "y": 0, "width": 1},
                {"op": "no_such_op"},
                {"op": "fill", "ids": ["r1"], "select": {"tag": "rect"}, "color": "#fff"},
                {"op": "move", "ids": ["r1"], "dx": 1, "dy": 1, "bogus": 1},
            ]
        )
    msg = str(exc.value)
    assert msg.startswith("invalid ops: ops[0].create_rect.x")
    assert msg.count(";") == 2 and "more)" in msg


def test_targeted_op_needs_exactly_one_of_ids_or_select() -> None:
    with pytest.raises(OpError, match="exactly one of 'ids' or 'select'"):
        parse_ops([{"op": "fill", "color": "#fff"}])
    with pytest.raises(OpError, match="exactly one of 'ids' or 'select'"):
        parse_ops([{"op": "fill", "ids": ["a"], "select": {"tag": "rect"}, "color": "#fff"}])


# --- risk / approval ----------------------------------------------------------


def test_delete_requires_approval_token(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    with pytest.raises(
        OpError, match=r"ops\[1\] delete is HIGH risk; re-call with approval_token"
    ) as ei:
        run_ops(
            doc_id,
            {"op": "fill", "ids": ["r1"], "color": "#0000ff"},
            {"op": "delete", "ids": ["t1"]},
        )
    assert "or send the other 1 op(s) without ops[1] as a medium call that needs no token" in str(
        ei.value
    )
    assert sandbox.working_copy(root, doc_id).read_bytes() == before
    assert _records(root, doc_id) == []


def test_risk_is_the_max_over_ops(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    result = run_ops(
        doc_id,
        {"op": "fill", "ids": ["r1"], "color": "#0000ff"},
        {"op": "delete", "ids": ["t1"]},
        approval_token=TOKEN,
    )
    assert result.risk == "high" and result.needs_approval is True
    assert not _has(_working(root, doc_id), "t1")
    (record,) = _records(root, doc_id)
    assert record["risk_class"] == "high"


# --- $n references ------------------------------------------------------------


def test_refs_in_parent_ids_and_href(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    result = run_ops(
        doc_id,
        {"op": "create_group", "id": "logo"},
        {"op": "create_rect", "x": 0, "y": 0, "width": 5, "height": 5, "parent": "$0", "id": "in"},
        {"op": "create_use", "href": "$1", "x": 10, "y": 0},
        {"op": "fill", "ids": ["$1"], "color": "#00f"},
    )
    working = _working(root, doc_id)
    assert _find(working, "in").getparent() is _find(working, "logo")
    use = next(e for e in working.iter() if isinstance(e.tag, str) and e.tag.endswith("}use"))
    assert "#in" in use.attrib.values()
    assert _style(_find(working, "in"))["fill"] == "#00f"
    assert result.created[0].id == "logo" and result.created[1].id == "in"


def test_ref_in_a_paint_uses_a_gradient_created_in_the_same_call(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    run_ops(
        doc_id,
        {
            "op": "linear_gradient",
            "id": "sky",
            "stops": [{"offset": 0, "color": "#fff"}, {"offset": 1, "color": "#00f"}],
        },
        {"op": "fill", "ids": ["r1"], "color": "url(#$0)"},
    )
    assert _style(_find(_working(root, doc_id), "r1"))["fill"] == "url(#sky)"


def test_ref_in_scope_and_group_ids(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    run_ops(
        doc_id,
        {"op": "create_circle", "cx": 5, "cy": 5, "r": 2, "id": "c", "fill": "#ff0000"},
        {"op": "group", "ids": ["$0", "r1"], "id": "g"},
        {"op": "replace_color", "from_color": "#ff0000", "to_color": "#00ff00", "ids": ["$1"]},
    )
    working = _working(root, doc_id)
    assert {e.get("id") for e in _find(working, "g")} == {"c", "r1"}
    c = _find(working, "c")
    assert "#00ff00" in (c.get("fill"), _style(c).get("fill"))
    assert _find(working, "r2").get("fill") == "#ff0000"  # outside the scope


def test_forward_and_non_creating_refs_are_rejected(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    with pytest.raises(OpError, match=r"ops\[0\] fill: \$1 must reference an EARLIER op"):
        run_ops(
            doc_id,
            {"op": "fill", "ids": ["$1"], "color": "#fff"},
            {"op": "create_circle", "cx": 1, "cy": 1, "r": 1},
        )
    with pytest.raises(OpError, match=r"ops\[1\] fill: \$0 refers to op 0 \(move\) which creates"):
        run_ops(
            doc_id,
            {"op": "move", "ids": ["r1"], "dx": 1, "dy": 1},
            {"op": "fill", "ids": ["$0"], "color": "#fff"},
        )
    assert sandbox.working_copy(root, doc_id).read_bytes() == before


# --- selectors ----------------------------------------------------------------


def test_selector_fans_out_over_matches(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    result = run_ops(doc_id, {"op": "move", "select": {"tag": "rect"}, "dx": 3, "dy": 0})
    working = _working(root, doc_id)
    assert _find(working, "r1").get("transform") == "translate(3,0)"
    assert _find(working, "r2").get("transform") == "translate(3,0)"
    assert result.outcomes[0].matched == ["r1", "r2"]


def test_selector_sees_objects_created_earlier_in_the_call(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    run_ops(
        doc_id,
        {
            "op": "create_rect",
            "x": 0,
            "y": 0,
            "width": 4,
            "height": 4,
            "id": "n",
            "fill": "#00ff00",
        },
        {"op": "fill", "select": {"fill": "#00ff00"}, "color": "#0000ff"},
    )
    assert _style(_find(_working(root, doc_id), "n"))["fill"] == "#0000ff"


def test_selector_with_no_match_or_too_many_is_an_error(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    with pytest.raises(OpError, match=r"ops\[0\] fill: select matched 0 objects"):
        run_ops(doc_id, {"op": "fill", "select": {"tag": "ellipse"}, "color": "#fff"})
    with pytest.raises(
        OpError,
        match=r"more than max_matches=1; .*raise select\.max_matches "
        r"\(a field inside 'select', up to 1024\)",
    ):
        run_ops(
            doc_id, {"op": "fill", "select": {"tag": "rect", "max_matches": 1}, "color": "#fff"}
        )
    assert sandbox.working_copy(root, doc_id).read_bytes() == before


# --- dry run -------------------------------------------------------------------


def test_dry_run_returns_the_plan_and_writes_nothing(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    result = run_ops(
        doc_id,
        {"op": "create_rect", "x": 0, "y": 0, "width": 4, "height": 4, "id": "n"},
        {"op": "fill", "select": {"tag": "rect"}, "color": "#0000ff"},
        {"op": "delete", "ids": ["t1"]},
        dry_run=True,
    )
    assert result.dry_run is True and result.changed is False
    assert result.risk == "high" and result.needs_approval is True
    assert result.outcomes[1].matched == ["r1", "r2", "n"]
    assert result.created[0].id == "n"
    assert sandbox.working_copy(root, doc_id).read_bytes() == before
    assert _snapshot_count(root, doc_id) == 0 and _records(root, doc_id) == []


# --- path ops gate ------------------------------------------------------------------


def test_path_ops_are_refused_when_raw_actions_are_disabled(
    doc: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _ = doc
    monkeypatch.setenv(ENV_RAW_ACTION_ENABLED, "0")
    get_settings.cache_clear()
    with pytest.raises(OpError, match=r"ops\[0\] simplify: path ops are disabled"):
        run_ops(doc_id, {"op": "simplify", "ids": ["r1"]}, approval_token=TOKEN)


# --- inline render ---------------------------------------------------------------


def test_render_true_renders_exactly_once(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    result = run_ops(doc_id, {"op": "fill", "ids": ["r1"], "color": "#0000ff"}, render=True)
    assert _KERNEL_RENDERS == [doc_id] and _PIPELINE_RENDERS == []
    assert result.render_artifact is not None
    assert (root / result.render_artifact).is_file()


def test_render_reuses_the_after_preview_when_the_pipeline_made_one(
    doc: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root = doc
    monkeypatch.setenv(ENV_EDIT_PREVIEWS, "after")
    get_settings.cache_clear()
    result = run_ops(doc_id, {"op": "fill", "ids": ["r1"], "color": "#0000ff"}, render=True)
    assert _PIPELINE_RENDERS == [doc_id] and _KERNEL_RENDERS == []
    assert result.render_artifact is not None
    assert (root / result.render_artifact).is_file()


# --- engine-delegating ops -----------------------------------------------------------


def test_place_reads_the_current_in_memory_state_for_the_same_document(
    doc: tuple[str, Path],
) -> None:
    doc_id, root = doc
    result = run_ops(
        doc_id,
        {"op": "create_rect", "x": 0, "y": 0, "width": 3, "height": 3, "id": "fresh"},
        {"op": "place", "source_doc_id": doc_id, "source_id": "fresh", "x": 40, "y": 40},
    )
    placed = result.created[-1].id
    assert placed.startswith("placed-")
    assert _find(_working(root, doc_id), placed).get("transform") == "translate(40,40)"


# --- $n: multi-create primary, runtime "created no id", dry run, paint refs ----------------------


@pytest.mark.parametrize(
    "creator",
    [
        {"op": "duplicate", "select": {"tag": "rect"}},
        {"op": "tile", "select": {"tag": "rect"}, "rows": 1, "cols": 2, "dx": 40, "dy": 0},
    ],
)
def test_multi_create_ref_resolves_to_the_first_new_id_in_document_order(
    doc: tuple[str, Path], creator: dict[str, Any]
) -> None:
    """`$n` of an op that creates several objects = its `primary`: the first NEW id in doc order."""
    doc_id, root = doc
    result = run_ops(doc_id, creator, {"op": "fill", "ids": ["$0"], "color": "#0000ff"})
    new_ids = result.outcomes[0].created_ids
    assert len(new_ids) == 2
    (ref,) = result.created
    assert ref.op == 0 and ref.id == new_ids[0]
    assert result.outcomes[1].matched == [new_ids[0]]
    working = _working(root, doc_id)
    assert _style(_find(working, new_ids[0]))["fill"] == "#0000ff"
    assert _style(_find(working, new_ids[1])).get("fill") != "#0000ff"


def test_ref_to_an_op_that_created_no_id_at_runtime_is_an_atomic_error(
    doc: tuple[str, Path],
) -> None:
    """A 1x1 tile passes the static check (tile CREATES) but adds no clone, so `$0` is unbound."""
    doc_id, root = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    with pytest.raises(OpError, match=r"ops\[1\] fill: \$0 refers to op 0 \(tile\) which created"):
        run_ops(
            doc_id,
            {"op": "tile", "ids": ["r1"], "rows": 1, "cols": 1, "dx": 5, "dy": 5},
            {"op": "fill", "ids": ["$0"], "color": "#0000ff"},
        )
    _assert_untouched(root, doc_id, before)


def test_dry_run_resolves_refs_without_writing(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    result = run_ops(
        doc_id,
        {"op": "create_rect", "x": 0, "y": 0, "width": 4, "height": 4, "id": "n"},
        {"op": "fill", "ids": ["$0"], "color": "#0000ff"},
        dry_run=True,
    )
    assert result.dry_run is True and result.changed is False
    assert result.outcomes[1].matched == ["n"]
    assert sandbox.working_copy(root, doc_id).read_bytes() == before
    assert _snapshot_count(root, doc_id) == 0 and _records(root, doc_id) == []


def test_url_ref_in_a_create_paint_points_at_a_gradient_from_an_earlier_op(
    doc: tuple[str, Path],
) -> None:
    doc_id, root = doc
    stops = [{"offset": 0, "color": "#fff"}, {"offset": 1, "color": "#000"}]
    run_ops(
        doc_id,
        {"op": "radial_gradient", "id": "glow", "stops": stops},
        {
            "op": "create_rect",
            "x": 0,
            "y": 0,
            "width": 5,
            "height": 5,
            "id": "n",
            "fill": "url(#$0)",
        },
        {"op": "stroke", "ids": ["$1"], "color": "url( #$0 )"},
    )
    n = _find(_working(root, doc_id), "n")
    assert "url(#glow)" in (n.get("fill"), _style(n).get("fill"))
    assert _style(n)["stroke"] == "url(#glow)"


def test_url_ref_to_a_later_op_is_rejected_statically(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    with pytest.raises(OpError, match=r"ops\[0\] fill: \$1 must reference an EARLIER op"):
        run_ops(
            doc_id,
            {"op": "fill", "ids": ["r1"], "color": "url(#$1)"},
            {"op": "linear_gradient", "stops": [{"offset": 0, "color": "#fff"}]},
        )
    _assert_untouched(root, doc_id, before)


# --- atomicity after a SUCCESSFUL engine op -----------------------------------------------------


def test_later_failure_after_a_successful_path_op_persists_nothing(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    before = sandbox.working_copy(root, doc_id).read_bytes()
    with pytest.raises(OpError, match=r"ops\[1\] fill: "):
        run_ops(
            doc_id,
            {"op": "union", "ids": ["r1", "r2"]},
            {"op": "fill", "ids": ["pa"], "color": "notacolor;}"},
            approval_token=TOKEN,
        )
    _assert_untouched(root, doc_id, before)
    assert [r["status"] for r in _records(root, doc_id)] == ["discarded"]


# --- Operation Record content + mixed no-op ---------------------------------------------------


def test_record_params_carry_the_calls_op_list(doc: tuple[str, Path]) -> None:
    """The audit trail is the op count + op names in call order (field values are not stored)."""
    doc_id, root = doc
    run_ops(
        doc_id,
        {"op": "fill", "ids": ["r1"], "color": "#0000ff"},
        {"op": "create_rect", "x": 0, "y": 0, "width": 4, "height": 4},
        {"op": "move", "ids": ["$1"], "dx": 1},
    )
    (record,) = _records(root, doc_id)
    assert record["params"] == {"op_count": 3, "ops": ["fill", "create_rect", "move"]}


def test_one_real_op_plus_a_noop_is_one_change(doc: tuple[str, Path]) -> None:
    doc_id, root = doc
    result = run_ops(
        doc_id,
        {"op": "fill", "ids": ["r1"], "color": "#ff0000"},  # already red: no-op
        {"op": "fill", "ids": ["r2"], "color": "#0000ff"},
    )
    assert result.changed is True and result.snapshot_id
    (record,) = _records(root, doc_id)
    assert record["status"] == "applied" and record["snapshot_id"] == result.snapshot_id
    assert _snapshot_count(root, doc_id) == 1
