"""Path ops of `edit` (simplify, union, difference, combine, break_apart, stroke_to_path, cleanup).

The path ops are HIGH-risk and run Inkscape on the CURRENT in-memory tree. Hermetic: the engine's
`run_inkscape` is replaced by `FakeEngine`, which writes a canned SVG to the engine's temp
``--export-filename`` so the real argv assembly, output re-parse and DOM-replace bridge run end to
end. Covers: the Action each op runs, the approval gate, dry run, id validation (charset,
existence, binary arity), argv safety, reversibility, engine failures, the surviving id of merge
ops, the explicit fill / empty-marker clean-up of `stroke_to_path`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from lxml import etree

from inkscape_mcp.edit import paths as engine
from inkscape_mcp.edit.dom import EditError
from inkscape_mcp.edit.ops import OpError
from inkscape_mcp.operations import OperationStatus, get_operation
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.subprocess_exec import ProcessError, ProcessResult
from tests.conftest import run_ops
from tests.tool_support import open_doc

SVG_NS = "http://www.w3.org/2000/svg"
TOKEN = "approved-by-operator"
HEAD = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'

#: two overlapping squares-as-paths so the boolean ops have >= 2 distinct targets
PATHS = (
    HEAD + b'<path id="p1" d="M10,10 L50,10 L50,50 L10,50 Z" fill="red"/>'
    b'<path id="p2" d="M30,30 L70,30 L70,70 L30,70 Z" fill="blue"/></svg>'
)
#: what the engine returns for most ops: one merged path that keeps the BOTTOM id `p1`
MERGED = (
    HEAD + b'<path id="p1" d="M10,10 L50,10 L50,30 L70,30 L70,70 L30,70 L30,50 L10,50 Z"/></svg>'
)
#: Inkscape's `path-combine` keeps the TOP id `p2`; a `<use>` references it
COMBINED = (
    HEAD + b'<path id="p2" d="M10,10 L50,10 L50,50 L10,50 Z M30,30 L70,30 L70,70 L30,70 Z"/>'
    b'<use xlink:href="#p2" xmlns:xlink="http://www.w3.org/1999/xlink"/></svg>'
)


class FakeEngine:
    """Stand-in for `run_inkscape` in the path engine; records every argv it is handed."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []
        self.output: bytes | None = None  # overrides the canned output
        self.returncode = 0
        self.timed_out = False
        self.error: Exception | None = None

    def __call__(self, args: list[str], settings: object | None = None) -> ProcessResult:
        self.argvs.append(list(args))
        if self.error is not None:
            raise self.error
        actions = next(a for a in args if a.startswith("--actions="))
        payload = self.output or (COMBINED if "path-combine" in actions else MERGED)
        if self.returncode == 0 and not self.timed_out:
            out = next(a for a in args if a.startswith("--export-filename="))
            Path(out.split("=", 1)[1]).write_bytes(payload)
        return ProcessResult(
            args=list(args),
            returncode=self.returncode,
            stdout="",
            stderr="",
            duration_s=0.0,
            timed_out=self.timed_out,
        )

    @property
    def actions(self) -> str:
        return next(a for a in self.argvs[-1] if a.startswith("--actions="))


@pytest.fixture
def eng(monkeypatch: pytest.MonkeyPatch, tool_ws: Path, tool_renders: list[str]) -> FakeEngine:
    fake = FakeEngine()
    monkeypatch.setattr(engine, "run_inkscape", fake)
    return fake


@pytest.fixture
def doc(eng: FakeEngine, tool_ws: Path) -> str:
    return open_doc(tool_ws, "shapes.svg", PATHS)


def _working(ws: Path, doc_id: str) -> etree._Element:
    return etree.fromstring(sandbox.working_copy(ws, doc_id).read_bytes())


def _bytes(ws: Path, doc_id: str) -> bytes:
    return sandbox.working_copy(ws, doc_id).read_bytes()


def _ids(root: etree._Element) -> set[str]:
    return {e.get("id") for e in root.iter() if isinstance(e.tag, str) and e.get("id")}


def _records(ws: Path, doc_id: str) -> list[dict[str, Any]]:
    d = sandbox.operations_dir(ws, doc_id)
    return [json.loads(p.read_text()) for p in sorted(d.glob("op_*.json"))] if d.is_dir() else []


def _untouched(ws: Path, doc_id: str, before: bytes) -> None:
    assert _bytes(ws, doc_id) == before
    assert not sandbox.snapshots_index(ws, doc_id).is_file()
    assert {r["status"] for r in _records(ws, doc_id)} <= {"discarded"}


def _merge(op: str, *ids: str) -> dict[str, Any]:
    return {"op": op, "ids": list(ids)}


UNION = _merge("union", "p1", "p2")


# --- every op runs its fixed Inkscape Action ---------------------------------


@pytest.mark.parametrize(
    ("op", "ids", "action"),
    [
        ("simplify", ["p1"], "path-simplify"),
        ("union", ["p1", "p2"], "path-union"),
        ("difference", ["p1", "p2"], "path-difference"),
        ("combine", ["p1", "p2"], "path-combine"),
        ("break_apart", ["p1"], "path-break-apart"),
        ("stroke_to_path", ["p1"], "object-stroke-to-path"),
        ("cleanup", ["p1"], "path-simplify"),
    ],
)
def test_each_op_applies_runs_its_action_and_is_recorded(
    eng: FakeEngine, doc: str, tool_ws: Path, op: str, ids: list[str], action: str
) -> None:
    res = run_ops(doc, _merge(op, *ids), approval_token=TOKEN)
    assert res.changed and res.risk == "high" and res.needs_approval
    assert eng.actions == f"--actions=select-by-id:{','.join(ids)};{action}"
    record = get_operation(doc, res.operation_id)
    assert record.status is OperationStatus.APPLIED
    assert record.risk_class.value == "high" and record.policy_decision["approved"] is True
    assert record.snapshot_id == res.snapshot_id


def test_applied_op_keeps_original_snapshots_and_previews(
    eng: FakeEngine, doc: str, tool_ws: Path, before_after_previews: None
) -> None:
    source = tool_ws / "shapes.svg"
    pre_op = _bytes(tool_ws, doc)
    res = run_ops(doc, UNION, approval_token=TOKEN)
    assert len(_working(tool_ws, doc).findall(f".//{{{SVG_NS}}}path")) == 1
    assert set(_records(tool_ws, doc)[0]["previews"]) == {"before", "after"}
    assert source.read_bytes() == PATHS  # the original file is never written
    restore_snapshot(doc, res.snapshot_id)
    assert _bytes(tool_ws, doc) == pre_op


# --- approval gate / dry run ------------------------------------------------


@pytest.mark.parametrize("token", [None, "", "   "])
def test_real_run_without_a_token_is_refused_and_runs_nothing(
    eng: FakeEngine, doc: str, tool_ws: Path, token: str | None
) -> None:
    # every path op shares one risk gate (test_each_op_... asserts each is HIGH): one op suffices
    before = _bytes(tool_ws, doc)
    with pytest.raises(OpError, match=r"ops\[0\] union is HIGH risk; re-call with approval_token"):
        run_ops(doc, UNION, approval_token=token)
    assert eng.argvs == [] and _records(tool_ws, doc) == []
    assert _bytes(tool_ws, doc) == before


def test_dry_run_plans_without_the_engine_or_a_token(
    eng: FakeEngine, doc: str, tool_ws: Path
) -> None:
    before = _bytes(tool_ws, doc)
    res = run_ops(doc, UNION, dry_run=True)
    assert res.dry_run and not res.changed and res.needs_approval
    assert res.operation_id == "" and res.snapshot_id == ""
    assert res.outcomes[0].matched == ["p1", "p2"]
    assert res.outcomes[0].summary.startswith("dry-run: would apply boolean_union")
    assert res.created[0].id == "p1"  # the predicted surviving id
    assert res.outcomes[0].summary.endswith("result keeps id p1, style from p1")
    comb = run_ops(doc, _merge("combine", "p1", "p2"), dry_run=True).outcomes[0].summary
    assert "result keeps id p1, style from p2" in comb  # combine keeps the TOP input's style
    assert comb.endswith("(fill red \u2192 blue)")  # ...and the visible change is spelled out
    assert eng.argvs == [] and _records(tool_ws, doc) == []
    assert _bytes(tool_ws, doc) == before
    assert (tool_ws / "shapes.svg").read_bytes() == PATHS


def test_dry_run_still_validates_targets(eng: FakeEngine, doc: str) -> None:
    with pytest.raises(OpError, match=r"ops\[0\] simplify: id 'nope' not found"):
        run_ops(doc, _merge("simplify", "nope"), dry_run=True)
    with pytest.raises(OpError, match="at least two distinct"):
        run_ops(doc, _merge("union", "p1"), dry_run=True)


# --- target validation ------------------------------------------------------


@pytest.mark.parametrize("bad_id", ["a b", "p1;rm -rf", "../escape", 'p"1', "p1 p2", "-p1", "n:s"])
def test_unsafe_ids_never_reach_the_engine(
    eng: FakeEngine, doc: str, tool_ws: Path, bad_id
) -> None:
    before = _bytes(tool_ws, doc)
    with pytest.raises(OpError, match=r"ops\[0\] simplify: "):
        run_ops(doc, _merge("simplify", bad_id), approval_token=TOKEN)
    assert eng.argvs == []
    _untouched(tool_ws, doc, before)


@pytest.mark.parametrize(
    ("ids", "message"),
    [
        ([], "no target object ids supplied"),
        (["a b"], "object id is not a safe svg id"),
        (["n:s"], "must not contain ':'"),
        (["ghost"], "object id not found in document"),
    ],
)
def test_validate_targets_checks_charset_before_existence(ids: list[str], message: str) -> None:
    root = etree.fromstring(PATHS)
    exc = EditError if "found" not in message else engine.TargetNotFound
    with pytest.raises(exc, match=message):
        engine.validate_targets(root, ids, engine.SIMPLIFY)


def test_validate_targets_dedupes_and_keeps_order() -> None:
    root = etree.fromstring(PATHS)
    assert engine.validate_targets(root, ["p2", "p1", "p2"], engine.UNION) == ["p2", "p1"]


@pytest.mark.parametrize("ids", [["p1"], ["p1", "p1"]])
def test_binary_ops_need_two_distinct_ids(eng: FakeEngine, doc: str, ids: list[str]) -> None:
    with pytest.raises(OpError, match=r"ops\[0\] union: boolean_union requires at least two"):
        run_ops(doc, _merge("union", *ids), approval_token=TOKEN)
    assert eng.argvs == []


def test_a_selector_can_target_the_paths(eng: FakeEngine, doc: str) -> None:
    res = run_ops(doc, {"op": "union", "select": {"tag": "path"}}, approval_token=TOKEN)
    assert res.outcomes[0].matched == ["p1", "p2"]
    assert eng.actions == "--actions=select-by-id:p1,p2;path-union"


# --- argv security (sec.12) -------------------------------------------------


def test_argv_is_a_list_built_from_fixed_tokens_and_validated_ids(
    eng: FakeEngine, doc: str
) -> None:
    run_ops(doc, _merge("difference", "p1", "p2"), approval_token=TOKEN)
    argv = eng.argvs[0]
    assert eng.actions == "--actions=select-by-id:p1,p2;path-difference"
    id_part = eng.actions[len("--actions=select-by-id:") :].split(";", 1)[0]
    assert not re.search(r"[;&|`$><()\s]", id_part.replace(",", ""))
    # output goes to a server temp file, never a client path
    assert "inkscape-mcp-path-" in next(a for a in argv if a.startswith("--export-filename="))


def test_engine_input_is_the_current_tree_and_temp_files_are_removed(
    eng: FakeEngine, doc: str, tool_ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bytes] = []

    def spy(args: list[str], settings: object | None = None) -> ProcessResult:
        seen.append(Path(args[0]).read_bytes())
        return eng(args, settings)

    monkeypatch.setattr(engine, "run_inkscape", spy)
    run_ops(
        doc,
        {"op": "create_path", "d": "M0,0 L5,0 L5,5 Z", "id": "p3"},
        {"op": "simplify", "ids": ["$0"]},
        approval_token=TOKEN,
    )
    assert b'id="p3"' in seen[0]
    assert not list((sandbox.artifacts_dir(tool_ws, doc) / "tmp").glob("*.svg"))


# --- engine failures --------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ({"returncode": 1}, "path operation failed"),
        ({"timed_out": True}, "path operation timed out"),
        ({"output": b"  "}, "path operation produced empty output"),
        ({"error": ProcessError("no binary")}, "inkscape engine unavailable"),
    ],
)
def test_engine_failures_name_the_op_and_leave_no_partial_write(
    eng: FakeEngine, doc: str, tool_ws: Path, setup: dict[str, Any], message: str
) -> None:
    before = _bytes(tool_ws, doc)
    for key, value in setup.items():
        setattr(eng, key, value)
    with pytest.raises(OpError, match=rf"ops\[1\] union: {message}"):
        run_ops(doc, {"op": "fill", "ids": ["p1"], "color": "#fff"}, UNION, approval_token=TOKEN)
    _untouched(tool_ws, doc, before)


# --- the surviving id of merge ops ------------------------------------------


@pytest.mark.parametrize(
    ("op", "order"),
    [
        ("union", ["p1", "p2"]),
        ("union", ["p2", "p1"]),
        ("combine", ["p1", "p2"]),  # path-combine keeps the TOP id: renamed back to p1
        ("combine", ["p2", "p1"]),
    ],
)
def test_merge_ops_survive_the_bottom_id_whatever_the_argument_order(
    eng: FakeEngine, doc: str, op: str, order: list[str]
) -> None:
    res = run_ops(doc, _merge(op, *order), approval_token=TOKEN)
    assert [c.id for c in res.created] == ["p1"]


def test_combine_renames_the_top_id_and_rewrites_references(
    eng: FakeEngine, doc: str, tool_ws: Path
) -> None:
    run_ops(doc, _merge("combine", "p1", "p2"), approval_token=TOKEN)
    working = _working(tool_ws, doc)
    assert "p1" in _ids(working) and "p2" not in _ids(working)
    use = working.find(f".//{{{SVG_NS}}}use")
    assert use is not None and use.get("{http://www.w3.org/1999/xlink}href") == "#p1"


@pytest.mark.parametrize("op", ["union", "combine"])
def test_dry_run_predicts_the_same_surviving_id(eng: FakeEngine, doc: str, op: str) -> None:
    assert run_ops(doc, _merge(op, "p1", "p2"), dry_run=True).created[0].id == "p1"


@pytest.mark.parametrize("op", ["simplify", "cleanup"])
def test_simplify_and_cleanup_report_no_created_id(eng: FakeEngine, doc: str, op: str) -> None:
    assert run_ops(doc, _merge(op, "p1"), approval_token=TOKEN).created == []


def test_stroke_to_path_group_result_is_listed_once(eng: FakeEngine, doc: str) -> None:
    eng.output = (
        HEAD + b'<g id="p1"><path id="n1" d="M0 0 L1 1"/><path id="n2" d="M0 0"/></g></svg>'
    )
    res = run_ops(doc, _merge("stroke_to_path", "p1"), approval_token=TOKEN)
    assert [c.id for c in res.created] == ["p1"]


def test_break_apart_reports_every_piece_not_defs(eng: FakeEngine, doc: str) -> None:
    eng.output = (
        HEAD + b'<defs id="defs1"><path id="in-defs" d="M0 0"/></defs>'
        b'<path id="p1" d="M0 0 h1"/><path id="path9" d="M2 2 h1"/>'
        b'<path id="path10" d="M4 4 h1"/></svg>'
    )
    res = run_ops(doc, _merge("break_apart", "p1"), approval_token=TOKEN)
    assert [c.id for c in res.created] == ["p1", "path9", "path10"]


def test_break_apart_pieces_chain_as_dollar_refs(eng: FakeEngine, doc: str) -> None:
    eng.output = HEAD + b'<path id="p1" d="M0 0 h1"/><path id="path9" d="M2 2 h1"/></svg>'
    res = run_ops(
        doc,
        _merge("break_apart", "p1"),
        {"op": "fill", "ids": ["$0"], "color": "#fff"},
        approval_token=TOKEN,
    )
    assert [(c.op, c.id) for c in res.created] == [(0, "p1"), (0, "path9")]


def test_dry_run_stroke_to_path_predicts_the_id_without_the_engine(
    eng: FakeEngine, doc: str
) -> None:
    res = run_ops(doc, _merge("stroke_to_path", "p1"), dry_run=True)
    assert res.created[0].id == "p1" and eng.argvs == []


def test_dry_run_break_apart_cannot_predict_pieces(eng: FakeEngine, doc: str) -> None:
    res = run_ops(doc, _merge("break_apart", "p1"), dry_run=True)
    assert res.created == [] and eng.argvs == []


def test_stroke_to_path_result_chains_into_simplify(eng: FakeEngine, doc: str) -> None:
    eng.output = _outlined()
    run_ops(
        doc,
        _merge("stroke_to_path", "p1"),
        {"op": "simplify", "ids": ["$0"]},
        approval_token=TOKEN,
    )
    assert eng.actions == "--actions=select-by-id:p1;path-simplify"


@pytest.mark.inkscape
def test_real_break_apart_and_stroke_to_path_report_ids(tool_ws: Path) -> None:
    doc = open_doc(
        tool_ws,
        "real.svg",
        HEAD + b'<path id="cp" d="M0,0 h10 v10 h-10 Z M20,0 h10 v10 h-10 Z"/>'
        b'<circle id="ring" cx="60" cy="60" r="20" fill="none" stroke="#f4a261" '
        b'stroke-width="3"/></svg>',
    )
    apart = run_ops(doc, _merge("break_apart", "cp"), approval_token=TOKEN)
    ids = [c.id for c in apart.created]
    root = _working(tool_ws, doc)
    assert len(ids) == 2 and len(set(ids)) == 2 and set(ids) <= _ids(root)
    ring = run_ops(doc, _merge("stroke_to_path", "ring"), approval_token=TOKEN)
    assert [c.id for c in ring.created] == ["ring"]


def test_the_result_id_chains_into_the_next_boolean_in_one_call(eng: FakeEngine, doc: str) -> None:
    res = run_ops(
        doc,
        UNION,
        {"op": "create_path", "d": "M0,0 L5,0 L5,5 Z", "id": "p3"},
        {"op": "union", "ids": ["$0", "p3"]},
        approval_token=TOKEN,
    )
    assert [(c.op, c.id) for c in res.created] == [(0, "p1"), (1, "p3"), (2, "p1")]
    assert eng.actions == "--actions=select-by-id:p1,p3;path-union"


# --- stroke_to_path post-processing -----------------------------------------


def _outlined(extra: bytes = b"", fill: bytes = b"") -> bytes:
    return HEAD + b'<path id="p1" d="M0,8 L20,8 L20,12 L0,12 Z"' + fill + b"/>" + extra + b"</svg>"


def _effective_fill(elem: etree._Element) -> str:
    style = dict(p.split(":", 1) for p in (elem.get("style") or "").split(";") if ":" in p)
    return (style.get("fill") or elem.get("fill") or "").strip()


@pytest.mark.parametrize(
    ("source", "engine_fill", "expected"),
    [
        (b'fill="none" stroke="#0000ff" stroke-width="4"', b"", "#0000ff"),
        (b'style="stroke:#00ff00"', b"", "#00ff00"),
        (b'fill="red"', b"", "#000000"),  # no concrete stroke: explicit default black
        (b'stroke="#0000ff"', b' fill="#123456"', "#123456"),  # an explicit fill is kept
    ],
)
def test_stroke_to_path_writes_an_explicit_fill(
    eng: FakeEngine,
    tool_ws: Path,
    source: bytes,
    engine_fill: bytes,
    expected: str,
) -> None:
    doc = open_doc(
        tool_ws, "s.svg", HEAD + b'<path id="p1" d="M0,10 L20,10" ' + source + b"/></svg>"
    )
    eng.output = _outlined(fill=engine_fill)
    run_ops(doc, _merge("stroke_to_path", "p1"), approval_token=TOKEN)
    p1 = next(e for e in _working(tool_ws, doc).iter() if e.get("id") == "p1")
    assert _effective_fill(p1) == expected


def test_stroke_to_path_drops_empty_marker_stubs(eng: FakeEngine, doc: str, tool_ws: Path) -> None:
    eng.output = _outlined(b'<g id="empty-markers"></g><path id="empty-marker-path"/>')
    res = run_ops(doc, _merge("stroke_to_path", "p1"), approval_token=TOKEN)
    ids = _ids(_working(tool_ws, doc))
    assert "p1" in ids and not {"empty-markers", "empty-marker-path"} & ids
    assert [c.id for c in res.created] == ["p1"]


def test_stroke_to_path_keeps_a_real_marker(eng: FakeEngine, doc: str, tool_ws: Path) -> None:
    eng.output = _outlined(b'<g id="real-markers"><path id="m" d="M0 0 L1 1"/></g>')
    run_ops(doc, _merge("stroke_to_path", "p1"), approval_token=TOKEN)
    assert {"real-markers", "m"} <= _ids(_working(tool_ws, doc))


@pytest.mark.parametrize(("op", "style_from"), [("union", "p1"), ("combine", "p2")])
def test_merge_summary_names_kept_id_and_style_source(
    eng: FakeEngine, doc: str, op: str, style_from: str
) -> None:
    res = run_ops(doc, _merge(op, "p1", "p2"), approval_token=TOKEN)
    assert f"kept id p1, style from {style_from}" in res.outcomes[0].summary


def test_real_run_warnings_surface_style_effects(eng: FakeEngine, doc: str, tool_ws: Path) -> None:
    other = open_doc(tool_ws, "o.svg", PATHS)
    quiet = run_ops(other, _merge("union", "p1", "p2"), approval_token=TOKEN)
    assert quiet.warnings == []  # union keeps its own style: nothing surprising
    res = run_ops(doc, _merge("combine", "p1", "p2"), approval_token=TOKEN)
    assert "combine: result 'p1' now has the style of 'p2' (fill red \u2192 blue)" in res.warnings
    assert res.removed == ["p2"]  # the engine's top input is gone; the bottom id survives
    assert quiet.removed == ["p2"]  # union drops the other target too


def test_real_run_warnings_for_stroke_to_path_and_break_apart(
    eng: FakeEngine, tool_ws: Path
) -> None:
    doc = open_doc(
        tool_ws, "s.svg", HEAD + b'<path id="p1" d="M0,10 L20,10" stroke="#0000ff"/></svg>'
    )
    eng.output = _outlined()
    res = run_ops(doc, _merge("stroke_to_path", "p1"), approval_token=TOKEN)
    assert res.warnings == ["stroke_to_path: stroke colour is now the fill of p1"]
    eng.output = HEAD + b'<path id="p1" d="M0 0 h1"/><path id="path9" d="M2 2 h1"/></svg>'
    res = run_ops(doc, _merge("break_apart", "p1"), approval_token=TOKEN)
    assert any("holes are now separate filled pieces" in w for w in res.warnings)
    assert any("'p1' stays on the first piece (of 2, document order)" in w for w in res.warnings)
