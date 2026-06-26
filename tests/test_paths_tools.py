"""Path geometry tool + engine tests (E6-01 / ADR-002 / ADR-004 / ADR-005 / sec.12).

Hermetic: both Inkscape touch points are monkeypatched so no test launches Inkscape —
`render_preview` in the pipeline (before/after frames) and `run_inkscape` in the path engine (the
geometry op). The fake `run_inkscape` writes a deterministic mutated SVG to the engine's temp
``--export-filename`` and returns a success `ProcessResult`, so the real arg-list assembly,
output re-parse, and DOM-replace bridge are exercised end to end.

Coverage: each tool happy path (applied), HIGH-risk refusal without an approval token, dry-run
produces no mutation, object-id charset + existence validation, binary-op arity, reversibility,
arg-list construction (no shell string; validated ids only), and registration on the app.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from lxml import etree

from inkscape_mcp.config import ENV_WORKSPACE_ROOTS, get_settings
from inkscape_mcp.edit import paths as engine
from inkscape_mcp.edit import pipeline
from inkscape_mcp.registry import get_registry, reset_registry
from inkscape_mcp.render.cli import RenderResult
from inkscape_mcp.server import mcp
from inkscape_mcp.snapshots import restore_snapshot
from inkscape_mcp.tools.paths import (
    boolean_difference,
    boolean_union,
    break_apart,
    cleanup_paths,
    combine_paths,
    simplify_path,
    stroke_to_path,
)
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.subprocess_exec import ProcessError, ProcessResult

SVG_NS = "http://www.w3.org/2000/svg"

#: Two overlapping rectangles-as-paths so boolean ops have >= 2 distinct targets.
SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
    b'<path id="p1" d="M10,10 L50,10 L50,50 L10,50 Z" fill="red"/>'
    b'<path id="p2" d="M30,30 L70,30 L70,70 L30,70 Z" fill="blue"/>'
    b"</svg>"
)

#: The deterministic "mutated" SVG the fake engine writes (a single merged path). For union /
#: difference Inkscape keeps the BOTTOM (first-in-document) id `p1`, so the canned output does too.
ENGINE_OUTPUT = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
    b'<path id="p1" d="M10,10 L50,10 L50,30 L70,30 L70,70 L30,70 L30,50 L10,50 Z" fill="red"/>'
    b"</svg>"
)

#: Inkscape's ``path-combine`` keeps the TOP (last-in-document) id `p2`. This canned output mimics
#: that so the engine's id-survival NORMALIZATION (rename the survivor back to the bottom id) is
#: exercised. A `<use>` reference to `p2` checks the rename rewrites references too.
ENGINE_OUTPUT_COMBINE = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
    b'<path id="p2" d="M10,10 L50,10 L50,50 L10,50 Z M30,30 L70,30 L70,70 L30,70 Z" fill="red"/>'
    b'<use xlink:href="#p2" xmlns:xlink="http://www.w3.org/1999/xlink"/>'
    b"</svg>"
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-preview"

#: Captured argv from the last fake `run_inkscape` call (for the arg-list security assertions).
_LAST_ARGS: list[str] = []


@pytest.fixture
def doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path, Path]:
    """Open the two-path fixture; return (doc_id, owning_root, original_source_path)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    src = ws / "shapes.svg"
    src.write_bytes(SVG)
    entry = get_registry().open_document(str(src))
    return entry.doc_id, ws, src


@pytest.fixture(autouse=True)
def fake_render(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace `render_preview` in the pipeline with a hermetic fake (no Inkscape)."""

    def fake_render_preview(
        doc_id: str, width_px: int | None = None, settings: object | None = None
    ) -> RenderResult:
        entry = get_registry().get(doc_id)
        root = Path(entry.root)
        preview_dir = sandbox.artifacts_dir(root, doc_id) / "preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        out = preview_dir / "preview-auto.png"
        out.write_bytes(PNG_BYTES)
        # E11-01 one-location contract: artifact_path is workspace-ROOT-relative (matches the real
        # engine), so the pipeline's `root / artifact_path` join resolves correctly.
        rel = out.relative_to(root).as_posix()
        return RenderResult(
            doc_id=doc_id,
            artifact_path=rel,
            workspace_relative_path=rel,
            format="png",
            width_px=100,
            height_px=100,
            duration_s=0.01,
        )

    monkeypatch.setattr(pipeline, "render_preview", fake_render_preview)


@pytest.fixture(autouse=True)
def fake_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace `run_inkscape` in the path engine: write the canned output to the temp file."""
    _LAST_ARGS.clear()

    def fake_run_inkscape(args: list[str], settings: object | None = None) -> ProcessResult:
        _LAST_ARGS[:] = args
        # The last arg is `--export-filename=<temp>` — write the canned mutated SVG there. For a
        # combine op emit the variant that keeps the TOP id (p2) so the engine's id-survival
        # normalization back to the bottom id (p1) is exercised; every other op keeps p1 already.
        actions_arg = next((a for a in args if a.startswith("--actions=")), "")
        payload = ENGINE_OUTPUT_COMBINE if "path-combine" in actions_arg else ENGINE_OUTPUT
        out = next(
            a[len("--export-filename=") :] for a in args if a.startswith("--export-filename=")
        )
        Path(out).write_bytes(payload)
        return ProcessResult(
            args=list(args),
            returncode=0,
            stdout="",
            stderr="",
            duration_s=0.02,
            timed_out=False,
        )

    monkeypatch.setattr(engine, "run_inkscape", fake_run_inkscape)


def _working_root(root: Path, doc_id: str) -> etree._Element:
    return etree.parse(str(sandbox.working_copy(root, doc_id))).getroot()


def _operation_record(root: Path, doc_id: str, operation_id: str) -> dict:
    op_file = sandbox.operations_dir(root, doc_id) / f"{operation_id}.json"
    return json.loads(op_file.read_text())


TOKEN = "approved-by-operator"


# --- happy path (applied) ---------------------------------------------------


def test_boolean_union_applied_records_snapshot_and_previews(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, src = doc
    original = src.read_bytes()

    result = boolean_union(doc_id, ["p1", "p2"], dry_run=False, approval_token=TOKEN)

    # The working copy now holds the engine output (one path).
    paths = _working_root(root, doc_id).findall(f".//{{{SVG_NS}}}path")
    assert len(paths) == 1

    # Result + Operation Record: applied, HIGH risk, both previews linked.
    assert result.changed is True
    assert result.dry_run is False
    assert result.affected_ids == ["p1", "p2"]
    record = _operation_record(root, doc_id, result.operation_id)
    assert record["status"] == "applied"
    assert record["risk_class"] == "high"
    assert record["policy_decision"]["approved"] is True
    assert set(record["previews"]) == {"before", "after"}
    assert result.preview_before is not None
    assert result.preview_after is not None

    # The ORIGINAL source file is byte-unchanged.
    assert src.read_bytes() == original


@pytest.mark.parametrize(
    ("tool", "ids", "action"),
    [
        (simplify_path, ["p1"], "path-simplify"),
        (boolean_union, ["p1", "p2"], "path-union"),
        (boolean_difference, ["p1", "p2"], "path-difference"),
        (combine_paths, ["p1", "p2"], "path-combine"),
        (break_apart, ["p1"], "path-break-apart"),
        (stroke_to_path, ["p1"], "object-stroke-to-path"),
        (cleanup_paths, ["p1"], "path-simplify"),
    ],
)
def test_each_tool_applies_and_runs_expected_action(
    doc: tuple[str, Path, Path], tool, ids: list[str], action: str
) -> None:
    doc_id, root, _ = doc
    result = tool(doc_id, ids, dry_run=False, approval_token=TOKEN)
    assert result.changed is True
    assert result.operation_id is not None
    record = _operation_record(root, doc_id, result.operation_id)
    assert record["status"] == "applied"
    assert record["risk_class"] == "high"
    # The fixed Inkscape Action verb appears in the assembled --actions argv element.
    actions_arg = next(a for a in _LAST_ARGS if a.startswith("--actions="))
    assert action in actions_arg


# --- HIGH-risk refusal without an approval token ----------------------------


@pytest.mark.parametrize(
    "tool", [simplify_path, boolean_union, boolean_difference, stroke_to_path, cleanup_paths]
)
def test_real_run_without_token_refused_no_mutation(doc: tuple[str, Path, Path], tool) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()

    with pytest.raises(ToolError) as exc:
        tool(doc_id, ["p1", "p2"], dry_run=False, approval_token=None)
    assert "approval" in str(exc.value).lower()

    # Nothing mutated; no Operation Record written.
    assert working.read_bytes() == before
    op_dir = sandbox.operations_dir(root, doc_id)
    assert not op_dir.exists() or list(op_dir.glob("op_*.json")) == []


def test_empty_token_string_refused(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, _ = doc
    with pytest.raises(ToolError):
        boolean_union(doc_id, ["p1", "p2"], dry_run=False, approval_token="")


# --- dry-run: no mutation ---------------------------------------------------


def test_dry_run_is_default_and_writes_nothing(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, src = doc
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()
    original = src.read_bytes()

    # No dry_run arg -> defaults to True; no approval token needed.
    result = boolean_union(doc_id, ["p1", "p2"])

    assert result.dry_run is True
    assert result.changed is False
    assert result.operation_id is None
    assert result.snapshot_id is None
    assert result.affected_ids == ["p1", "p2"]
    assert result.summary is not None and "dry-run" in result.summary

    # Working copy + original untouched; no Operation Record exists.
    assert working.read_bytes() == before
    assert src.read_bytes() == original
    op_dir = sandbox.operations_dir(root, doc_id)
    assert not op_dir.exists() or list(op_dir.glob("op_*.json")) == []


def test_dry_run_still_validates_targets(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, _ = doc
    # Missing id is reported even in dry-run.
    with pytest.raises(ToolError) as exc:
        simplify_path(doc_id, ["nope"], dry_run=True)
    assert "object id not found" in str(exc.value)


# --- object-id validation ---------------------------------------------------


def test_missing_object_id_refused(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()
    with pytest.raises(ToolError) as exc:
        simplify_path(doc_id, ["does-not-exist"], dry_run=False, approval_token=TOKEN)
    assert "object id not found" in str(exc.value)
    assert working.read_bytes() == before


@pytest.mark.parametrize("bad_id", ["a b", "p1;rm -rf", "../escape", 'p"1', "p1 p2", "-p1"])
def test_unsafe_object_id_refused(doc: tuple[str, Path, Path], bad_id: str) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()
    with pytest.raises(ToolError) as exc:
        simplify_path(doc_id, [bad_id], dry_run=False, approval_token=TOKEN)
    # Either unsafe-charset or not-found, both before any engine run.
    assert "object id" in str(exc.value)
    assert working.read_bytes() == before


def test_empty_object_ids_refused(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, _ = doc
    with pytest.raises(ToolError) as exc:
        simplify_path(doc_id, [], dry_run=True)
    assert "no target object ids" in str(exc.value)


def test_binary_op_requires_two_ids(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, _ = doc
    with pytest.raises(ToolError) as exc:
        boolean_union(doc_id, ["p1"], dry_run=True)
    assert "at least two" in str(exc.value)


def test_binary_op_dedupes_repeated_id(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, _ = doc
    # ["p1", "p1"] de-dupes to one distinct id -> still fails the >= 2 arity rule.
    with pytest.raises(ToolError) as exc:
        boolean_union(doc_id, ["p1", "p1"], dry_run=True)
    assert "at least two" in str(exc.value)


# --- arg-list security (sec.12) ---------------------------------------------


def test_argv_is_a_list_with_validated_ids_only(doc: tuple[str, Path, Path]) -> None:
    doc_id, _, _ = doc
    boolean_difference(doc_id, ["p1", "p2"], dry_run=False, approval_token=TOKEN)

    # argv is a list (never a shell string); the actions element is composed only from the fixed
    # tokens + the validated ids — an exact match proves no client value was interpolated.
    assert isinstance(_LAST_ARGS, list)
    actions_arg = next(a for a in _LAST_ARGS if a.startswith("--actions="))
    assert actions_arg == "--actions=select-by-id:p1,p2;path-difference"
    # The only ';' is the fixed action separator; the id portion carries no shell metacharacters.
    id_portion = actions_arg[len("--actions=select-by-id:") :].split(";", 1)[0]
    assert id_portion == "p1,p2"
    assert not re.search(r"[;&|`$><()\s]", id_portion.replace(",", ""))
    # Output goes to a server temp file, never a client path.
    out_arg = next(a for a in _LAST_ARGS if a.startswith("--export-filename="))
    assert "inkscape-mcp-path-" in out_arg


# --- reversibility ----------------------------------------------------------


def test_reversibility_restore_snapshot_returns_pre_op_bytes(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    pre_op = working.read_bytes()

    result = boolean_union(doc_id, ["p1", "p2"], dry_run=False, approval_token=TOKEN)
    assert working.read_bytes() != pre_op

    assert result.snapshot_id is not None
    restore_snapshot(doc_id, result.snapshot_id)
    assert working.read_bytes() == pre_op


# --- engine failure mapping -------------------------------------------------


def test_engine_failure_maps_to_toolerror_no_partial_write(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, root, _ = doc
    working = sandbox.working_copy(root, doc_id)
    before = working.read_bytes()

    def failing(args: list[str], settings: object | None = None) -> ProcessResult:
        return ProcessResult(
            args=list(args),
            returncode=1,
            stdout="",
            stderr="boom",
            duration_s=0.01,
            timed_out=False,
        )

    monkeypatch.setattr(engine, "run_inkscape", failing)
    with pytest.raises(ToolError) as exc:
        boolean_union(doc_id, ["p1", "p2"], dry_run=False, approval_token=TOKEN)
    assert "path operation failed" in str(exc.value)
    # The pipeline discarded the op before writing the working copy.
    assert working.read_bytes() == before


def test_engine_unavailable_maps_to_toolerror(
    doc: tuple[str, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc_id, _, _ = doc

    def missing(args: list[str], settings: object | None = None) -> ProcessResult:
        raise ProcessError("inkscape binary not found")

    monkeypatch.setattr(engine, "run_inkscape", missing)
    with pytest.raises(ToolError) as exc:
        simplify_path(doc_id, ["p1"], dry_run=False, approval_token=TOKEN)
    assert "engine unavailable" in str(exc.value)


def test_unknown_doc_id_maps_to_toolerror(doc: tuple[str, Path, Path]) -> None:
    with pytest.raises(ToolError) as exc:
        simplify_path("d_nope", ["p1"], dry_run=True)
    assert "document id not found" in str(exc.value)


# --- id survival / result_id (E10-07 P7-vs-P4 / E11-07) ---------------------


def test_boolean_union_returns_bottom_result_id(doc: tuple[str, Path, Path]) -> None:
    doc_id, _root, _ = doc
    # p1 is first in document order (the bottom element) -> the standardized surviving id.
    result = boolean_union(doc_id, ["p1", "p2"], dry_run=False, approval_token=TOKEN)
    assert result.result_id == "p1"


def test_boolean_difference_returns_bottom_result_id(doc: tuple[str, Path, Path]) -> None:
    doc_id, _root, _ = doc
    result = boolean_difference(doc_id, ["p1", "p2"], dry_run=False, approval_token=TOKEN)
    assert result.result_id == "p1"


def test_combine_matches_boolean_bottom_id_rule(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    # Inkscape's path-combine keeps the TOP id (p2); the engine normalizes the result back to the
    # bottom id (p1) so combine and the boolean ops survive the SAME id (E10-07 P7-vs-P4).
    result = combine_paths(doc_id, ["p1", "p2"], dry_run=False, approval_token=TOKEN)
    assert result.result_id == "p1"
    optimized = _working_root(root, doc_id)
    ids = {e.get("id") for e in optimized.iter() if isinstance(e.tag, str)}
    assert "p1" in ids
    assert "p2" not in ids  # the top id was renamed to the bottom id, not left as a duplicate
    # The intra-document reference to the old top id was rewritten to the surviving id.
    use = optimized.find(f".//{{{SVG_NS}}}use")
    href = use.get("{http://www.w3.org/1999/xlink}href") if use is not None else None
    assert href == "#p1"


def test_combine_and_union_survive_identical_id(doc: tuple[str, Path, Path]) -> None:
    doc_id, _root, _ = doc
    # Both merge ops apply the SAME bottom-most-id rule on the same pristine inputs (dry-runs keep
    # the document intact, so the second op sees both targets too). E10-07 standardizes them.
    union = boolean_union(doc_id, ["p1", "p2"], dry_run=True)
    combine = combine_paths(doc_id, ["p1", "p2"], dry_run=True)
    assert union.result_id == "p1"
    assert combine.result_id == "p1"


def test_result_id_order_follows_document_not_argument_order(doc: tuple[str, Path, Path]) -> None:
    doc_id, _root, _ = doc
    # Even with p2 listed first in the argument, the surviving id is the first-in-DOCUMENT target.
    result = boolean_union(doc_id, ["p2", "p1"], dry_run=False, approval_token=TOKEN)
    assert result.result_id == "p1"


def test_result_id_chains_into_next_boolean_without_reinspect(doc: tuple[str, Path, Path]) -> None:
    doc_id, root, _ = doc
    first = boolean_union(doc_id, ["p1", "p2"], dry_run=False, approval_token=TOKEN)
    assert first.result_id == "p1"
    # The returned id is immediately present in the post-op document, so a chained boolean can
    # target it WITHOUT a re-inspect round-trip (E11-07): validate_targets resolves it against the
    # mutated working copy. Add a fresh sibling and chain a second union on the returned id.
    working = sandbox.working_copy(root, doc_id)
    tree = etree.parse(str(working))
    post_ids = {e.get("id") for e in tree.getroot().iter() if isinstance(e.tag, str)}
    assert first.result_id in post_ids  # immediately usable — no re-inspect needed

    extra = etree.SubElement(tree.getroot(), f"{{{SVG_NS}}}path")
    extra.set("id", "p3")
    extra.set("d", "M0,0 L5,0 L5,5 Z")
    working.write_bytes(etree.tostring(tree))

    # A dry run proves the chained op accepts the returned id as a live target (no re-inspect).
    second = boolean_union(doc_id, [first.result_id, "p3"], dry_run=True)
    assert second.affected_ids == [first.result_id, "p3"]
    assert second.result_id == "p1"  # p1 is still the bottom-most of {p1, p3}


def test_dry_run_previews_result_id(doc: tuple[str, Path, Path]) -> None:
    doc_id, _root, _ = doc
    result = boolean_union(doc_id, ["p1", "p2"], dry_run=True)
    # A dry run mutates nothing but still previews the id a real run would standardize to.
    assert result.changed is False
    assert result.result_id == "p1"


def test_non_merge_ops_have_no_result_id(doc: tuple[str, Path, Path]) -> None:
    doc_id, _root, _ = doc
    # simplify / break_apart / stroke_to_path are not merges — no single documented surviving id.
    for tool in (simplify_path, break_apart, stroke_to_path):
        result = tool(doc_id, ["p1"], dry_run=False, approval_token=TOKEN)
        assert result.result_id is None
        # restore for the next op so each runs against the same fixture geometry
        restore_snapshot(doc_id, result.snapshot_id)


# --- cleanup_paths overlap decision (E10-07 P10) ----------------------------


def test_cleanup_paths_aliases_simplify_same_action(doc: tuple[str, Path, Path]) -> None:
    doc_id, _root, _ = doc
    # Documented decision: cleanup_paths is a deliberate alias of simplify_path — both run the same
    # lossy `path-simplify` Action (no distinct lossless engine cleanup on Inkscape 1.4.3).
    cleanup = cleanup_paths(doc_id, ["p1"], dry_run=True)
    simplify = simplify_path(doc_id, ["p1"], dry_run=True)
    cleanup_action = engine._ACTIONS[engine.CLEANUP]
    simplify_action = engine._ACTIONS[engine.SIMPLIFY]
    assert cleanup_action == simplify_action == "path-simplify"
    # Neither is a merge op, so neither reports a result_id.
    assert cleanup.result_id is None
    assert simplify.result_id is None


# --- registration -----------------------------------------------------------


def test_tools_registered_on_mcp(doc: tuple[str, Path, Path]) -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {
        "simplify_path",
        "boolean_union",
        "boolean_difference",
        "combine_paths",
        "break_apart",
        "stroke_to_path",
        "cleanup_paths",
    } <= names


# --- E13-04: stroke_to_path writes an explicit fill (source stroke colour, not the default) ----


def _effective_fill(elem: etree._Element) -> str:
    style = dict(part.split(":", 1) for part in (elem.get("style") or "").split(";") if ":" in part)
    return (style.get("fill") or elem.get("fill") or "").strip()


def test_stroke_to_path_sets_explicit_fill_from_source_stroke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E13-04: the outlined result gets an EXPLICIT fill equal to the source stroke colour, so it
    no longer depends on the implicit SVG default black."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    src = ws / "stroked.svg"
    src.write_bytes(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
        b'<path id="s1" d="M0,10 L20,10" fill="none" stroke="#0000ff" stroke-width="4"/>'
        b"</svg>"
    )
    doc_id = get_registry().open_document(str(src)).doc_id

    # Canned engine output mimicking Inkscape `--export-plain-svg`: an outlined path with NO fill.
    outlined = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
        b'<path id="s1" d="M0,8 L20,8 L20,12 L0,12 Z"/>'
        b"</svg>"
    )

    def fake_engine_out(args: list[str], settings: object | None = None) -> ProcessResult:
        out = next(
            a[len("--export-filename=") :] for a in args if a.startswith("--export-filename=")
        )
        Path(out).write_bytes(outlined)
        return ProcessResult(
            args=list(args), returncode=0, stdout="", stderr="", duration_s=0.0, timed_out=False
        )

    monkeypatch.setattr(engine, "run_inkscape", fake_engine_out)

    result = stroke_to_path(doc_id, ["s1"], dry_run=False, approval_token=TOKEN)
    assert result.changed is True

    s1 = next(
        el
        for el in _working_root(ws, doc_id).iter()
        if isinstance(el.tag, str) and el.get("id") == "s1"
    )
    # The outlined path now carries an EXPLICIT fill = the source stroke colour (never the default).
    assert _effective_fill(s1) == "#0000ff"


def test_stroke_to_path_explicit_fill_defaults_black_without_source_stroke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no concrete source stroke, the outline still gets an EXPLICIT fill — black, the value it
    would otherwise rely on implicitly — rather than no fill at all."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(ws))
    get_settings.cache_clear()
    reset_registry()
    src = ws / "nostroke.svg"
    src.write_bytes(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
        b'<path id="s1" d="M0,10 L20,10" fill="red"/>'
        b"</svg>"
    )
    doc_id = get_registry().open_document(str(src)).doc_id
    outlined = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
        b'<path id="s1" d="M0,8 L20,8 L20,12 L0,12 Z"/>'
        b"</svg>"
    )

    def fake_engine_out(args: list[str], settings: object | None = None) -> ProcessResult:
        out = next(
            a[len("--export-filename=") :] for a in args if a.startswith("--export-filename=")
        )
        Path(out).write_bytes(outlined)
        return ProcessResult(
            args=list(args), returncode=0, stdout="", stderr="", duration_s=0.0, timed_out=False
        )

    monkeypatch.setattr(engine, "run_inkscape", fake_engine_out)
    stroke_to_path(doc_id, ["s1"], dry_run=False, approval_token=TOKEN)

    s1 = next(
        el
        for el in _working_root(ws, doc_id).iter()
        if isinstance(el.tag, str) and el.get("id") == "s1"
    )
    assert _effective_fill(s1) == "#000000"


# --- E14-08a: capability-absent path-op error names list_capabilities ---------


def test_path_op_error_message_engine_unavailable_names_list_capabilities() -> None:
    # E14-08a: a PathOpError signalling the engine is absent maps to a message naming the discovery
    # tool; any other PathOpError keeps its own already-safe message unchanged.
    from inkscape_mcp.edit.paths import PathOpError
    from inkscape_mcp.tools.paths import _path_op_error_message

    absent = _path_op_error_message(PathOpError("inkscape engine unavailable"))
    assert "list_capabilities" in absent

    other = _path_op_error_message(PathOpError("path operation timed out"))
    assert other == "path operation timed out"
