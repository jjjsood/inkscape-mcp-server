"""Path geometry tools: discrete, typed, HIGH-risk, reversible, dry-run-capable.

Seven small typed tools (ADR-002, no portmanteau) — ``simplify_path``, ``boolean_union``,
``boolean_difference``, ``combine_paths``, ``break_apart``, ``stroke_to_path``, ``cleanup_paths``
— that each run ONE destructive path geometry op through the Inkscape ENGINE (ADR-005; geometry is
real computation, not a DOM tweak) and route the result through the shared mutating pipeline
(:func:`inkscape_mcp.edit.pipeline.apply_edit`), so each change is uniformly snapshotted, recorded
as an Operation Record, and linked to a before/after preview (ADR-004) — and therefore reversible.

Path ops are DESTRUCTIVE and visually hard to evaluate (architecture §5.6), so every tool here is:

- **HIGH risk + approval-gated.** A real (non-dry-run) mutation requires a non-empty
  ``approval_token``; the pipeline passes ``RiskClass.HIGH`` to the policy gate, which refuses the
  op outright when the token is absent — no snapshot/preview/write ever runs unapproved (sec.12).
- **Dry-run-capable.** ``dry_run=True`` (the DEFAULT) validates the targets and reports exactly
  which object ids and which Inkscape Action WOULD run, WITHOUT invoking the engine, writing the
  working copy, or creating a snapshot/record-as-applied. Callers opt in to mutation explicitly.

This is a THIN layer: it builds the ``mutate`` closure over the pure engine in
``inkscape_mcp.edit.paths`` and maps engine exceptions to :class:`fastmcp.exceptions.ToolError`
with stable, host-path-free messages (sec.12).
"""

from __future__ import annotations

from pathlib import Path

from fastmcp.exceptions import ToolError
from lxml import etree
from pydantic import BaseModel

from inkscape_mcp.document.inspect import DocumentNotFound, InspectionError
from inkscape_mcp.edit import paths as engine
from inkscape_mcp.edit.dom import EditError, TargetNotFound, load_working_tree
from inkscape_mcp.edit.paths import PathOpError
from inkscape_mcp.edit.pipeline import EditApplyError, apply_edit
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.registry import DocEntry, get_registry
from inkscape_mcp.server import mcp
from inkscape_mcp.workspace.risk import PolicyViolation, RiskClass

_logger = get_logger("tools.paths")

#: The stable engine message (from `inkscape_mcp.edit.paths`) signalling the Inkscape engine could
#: not be launched on this runtime (capability ABSENT). Matched at the tool layer so the client-
#: facing error can NAME the discovery tool without the engine importing the tool surface.
_ENGINE_UNAVAILABLE = "inkscape engine unavailable"


def _path_op_error_message(exc: PathOpError) -> str:
    """Stable, host-path-free message for a `PathOpError`.

    When the engine is ABSENT (no Inkscape binary on this runtime) the message names the discovery
    tool so the agent can inspect support rather than retry blindly; every other `PathOpError`
    (timeout, failed op, unsafe output) keeps its own already-safe message.
    """
    if str(exc) == _ENGINE_UNAVAILABLE:
        return (
            "inkscape engine unavailable on this runtime; "
            "call list_capabilities to see what this runtime supports"
        )
    return str(exc)


class PathOpResult(BaseModel):
    """Outcome of one path-geometry tool call (dry-run OR applied).

    For a DRY RUN (`dry_run=True`): `changed` is False, `summary` describes the intended effect,
    and `operation_id` / `snapshot_id` / previews are all `None` (nothing was written). For an
    APPLIED run: `changed` is True and `operation_id`, `snapshot_id`, and the before/after preview
    paths (workspace-relative, or `None` when a render was unavailable) link the recorded,
    reversible operation. `affected_ids` lists the validated targets the op ran against.

    `result_id` is the id of the surviving merged element after a MERGE op (`boolean_union`,
    `boolean_difference`, `combine_paths`): the BOTTOM-most target — the one first in document
    order — per the standardized id-survival rule. It is immediately usable as an
    input to a chained boolean with no re-inspect. It is `None` for a dry run, for non-merge ops
    (e.g. `simplify_path`, `break_apart`, `stroke_to_path`), and if the engine produced no
    recognizable surviving target.
    """

    doc_id: str
    op: str
    dry_run: bool
    changed: bool
    affected_ids: list[str]
    result_id: str | None = None
    summary: str | None = None
    operation_id: str | None = None
    snapshot_id: str | None = None
    preview_before: str | None = None
    preview_after: str | None = None


def _run_path_tool(
    doc_id: str,
    op: str,
    object_ids: list[str],
    *,
    dry_run: bool,
    approval_token: str | None,
) -> PathOpResult:
    """Shared driver: dry-run (validate + describe, no mutation) or applied (HIGH-gated pipeline).

    Error mapping (sec.12, host-path-free):

    - unknown document -> "document id not found"
    - missing target object (`TargetNotFound`) -> "object id not found in document"
    - invalid input (`EditError`) -> its already-safe validation message
    - refused by policy (`PolicyViolation`, e.g. missing approval token) -> its safe message
    - engine failure/timeout/unsafe output (`PathOpError`) -> its safe message
    - unparseable working copy (`InspectionError`) -> "document could not be parsed safely"
    """
    if dry_run:
        return _dry_run(doc_id, op, object_ids)

    # Reject an unapproved high-risk mutation early with a clear message (the pipeline would also
    # refuse it via the policy gate, but failing here keeps the message tool-shaped).
    if not approval_token:
        raise ToolError("high-risk path operation requires an explicit approval_token")

    # Capture the engine-validated id list from inside the mutation, BEFORE the op runs — a
    # consuming op (union/difference/...) deletes its inputs, so the post-op tree can no longer be
    # re-queried for them. `result_holder` receives the surviving merged id (merge ops only).
    captured_ids: list[str] = []
    result_holder: list[str] = []

    def mutate(tree: etree._ElementTree) -> str:
        entry: DocEntry = get_registry().get(doc_id)
        captured_ids[:] = engine.validate_targets(tree.getroot(), object_ids, op)
        return engine.apply_path_op(
            tree, Path(entry.working_path), op, object_ids, result_holder=result_holder
        )

    try:
        result = apply_edit(
            doc_id,
            op,
            {"object_ids": object_ids, "dry_run": dry_run},
            mutate,
            approval_token=approval_token,
            risk_class=RiskClass.HIGH,
        )
    except (EditApplyError, DocumentNotFound, KeyError) as exc:
        raise ToolError("document id not found") from exc
    except TargetNotFound as exc:
        raise ToolError("object id not found in document") from exc
    except PolicyViolation as exc:
        raise ToolError(str(exc)) from exc
    except PathOpError as exc:
        raise ToolError(_path_op_error_message(exc)) from exc
    except EditError as exc:
        raise ToolError(str(exc)) from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc

    # Validated ids captured inside `mutate` (the post-op tree may no longer contain them).
    affected = captured_ids or object_ids
    result_id = result_holder[0] if result_holder else None

    log_tool_call(
        _logger,
        tool=op,
        doc_id=doc_id,
        operation_id=result.operation_id,
        snapshot_id=result.snapshot_id,
        result_id=result_id,
        risk_class=RiskClass.HIGH.value,
    )
    return PathOpResult(
        doc_id=doc_id,
        op=op,
        dry_run=False,
        changed=result.changed,
        affected_ids=affected,
        result_id=result_id,
        summary=result.summary,
        operation_id=result.operation_id,
        snapshot_id=result.snapshot_id,
        preview_before=result.preview_before,
        preview_after=result.preview_after,
    )


def _dry_run(doc_id: str, op: str, object_ids: list[str]) -> PathOpResult:
    """Validate the targets and report the intended effect WITHOUT mutating anything."""
    try:
        _entry, tree = load_working_tree(doc_id)
    except DocumentNotFound as exc:
        raise ToolError("document id not found") from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc

    root = tree.getroot()
    try:
        affected = engine.validate_targets(root, object_ids, op)
        summary = engine.describe_dry_run(root, op, object_ids)
    except TargetNotFound as exc:
        raise ToolError("object id not found in document") from exc
    except EditError as exc:
        raise ToolError(str(exc)) from exc

    # Predicted surviving id for a merge op (the bottom-most / first-in-document target). A dry run
    # never mutates, so this only previews the id a real run would standardize the result to.
    result_id = engine.predicted_result_id(root, op, affected)

    log_tool_call(_logger, tool=op, doc_id=doc_id, dry_run=True, risk_class=RiskClass.HIGH.value)
    return PathOpResult(
        doc_id=doc_id,
        op=op,
        dry_run=True,
        changed=False,
        affected_ids=affected,
        result_id=result_id,
        summary=summary,
    )


# --- Tools ------------------------------------------------------------------


@mcp.tool
def simplify_path(
    doc_id: str,
    object_ids: list[str],
    dry_run: bool = True,
    approval_token: str | None = None,
) -> PathOpResult:
    """Simplify the given path(s) via the Inkscape engine (``path-simplify``).

    When to use: reducing redundant nodes on path(s). `cleanup_paths` is an explicit alias of this;
    for LOSSLESS, non-destructive cleanup use `svg_web_optimize` instead.

    Key params: `object_ids` are the target paths. `dry_run=True` (DEFAULT) validates targets and
    reports which ids would be simplified WITHOUT invoking the engine; a real run needs a non-empty
    `approval_token`. Runs `select-by-id` + `path-simplify` and writes back over the working copy.

    Return shape: `PathOpResult` — `dry_run`, `changed`, `affected_ids`; on a real run also
    `operation_id`, `snapshot_id`, before/after preview (reversible via the pre-op snapshot).

    Example: `simplify_path(doc_id, ["curve"], dry_run=False, approval_token="ok")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: high (destructive geometry; approval-gated).
    """
    return _run_path_tool(
        doc_id, engine.SIMPLIFY, object_ids, dry_run=dry_run, approval_token=approval_token
    )


@mcp.tool
def boolean_union(
    doc_id: str,
    object_ids: list[str],
    dry_run: bool = True,
    approval_token: str | None = None,
) -> PathOpResult:
    """Union two or more paths into one via the Inkscape engine (``path-union``).

    When to use: fusing overlapping shapes into one outline. To SUBTRACT use `boolean_difference`;
    to merge keeping subpaths (not fused) use `combine_paths`.

    Key params: `object_ids` must be at least two distinct paths. `dry_run=True` (DEFAULT) reports
    which ids would be unioned (and the `result_id` it would produce) WITHOUT invoking the engine; a
    real run needs a non-empty `approval_token`. ID SURVIVAL: the merged result keeps ONE id — the
    BOTTOM-most target (first in document order) — so a chained boolean needs no re-inspect.

    Return shape: `PathOpResult` — `dry_run`, `changed`, `affected_ids`, `result_id` (surviving id);
    on a real run also `operation_id`, `snapshot_id`, before/after preview (reversible).

    Example: `boolean_union(doc_id, ["a", "b"], dry_run=False, approval_token="ok")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: high (destructive geometry; approval-gated).
    """
    return _run_path_tool(
        doc_id, engine.UNION, object_ids, dry_run=dry_run, approval_token=approval_token
    )


@mcp.tool
def boolean_difference(
    doc_id: str,
    object_ids: list[str],
    dry_run: bool = True,
    approval_token: str | None = None,
) -> PathOpResult:
    """Subtract the top path(s) from the bottom one via the Inkscape engine (``path-difference``).

    When to use: cutting one shape out of another. To MERGE shapes use `boolean_union`; to combine
    keeping subpaths use `combine_paths`.

    Key params: `object_ids` must be at least two distinct paths; Inkscape subtracts the upper
    path(s) from the lowest one (z-order). `dry_run=True` (DEFAULT) reports which ids would be
    differenced (and the `result_id`) WITHOUT invoking the engine; a real run needs a non-empty
    `approval_token`. ID SURVIVAL: the result keeps the BOTTOM-most target's id (the path subtracted
    FROM).

    Return shape: `PathOpResult` — `dry_run`, `changed`, `affected_ids`, `result_id` (surviving id);
    on a real run also `operation_id`, `snapshot_id`, before/after preview (reversible).

    Example: `boolean_difference(doc_id, ["base", "hole"], dry_run=False, approval_token="ok")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: high (destructive geometry; approval-gated).
    """
    return _run_path_tool(
        doc_id, engine.DIFFERENCE, object_ids, dry_run=dry_run, approval_token=approval_token
    )


@mcp.tool
def combine_paths(
    doc_id: str,
    object_ids: list[str],
    dry_run: bool = True,
    approval_token: str | None = None,
) -> PathOpResult:
    """Combine paths into a single multi-subpath path via the Inkscape engine (``path-combine``).

    When to use: merging paths while PRESERVING subpaths (fill-rule driven) rather than fusing
    outlines. To fuse outlines use `boolean_union`; to subtract use `boolean_difference`.

    Key params: `object_ids` must be at least two distinct paths. `dry_run=True` (DEFAULT) reports
    which ids would be combined (and the `result_id`) WITHOUT invoking the engine; a real run needs
    a non-empty `approval_token`. ID SURVIVAL: standardized to MATCH the boolean ops — the result
    keeps the BOTTOM-most target's id. Inkscape natively keeps the TOP id; this tool normalizes it
    back to the bottom one so combine and the booleans behave identically for a chained op.

    Return shape: `PathOpResult` — `dry_run`, `changed`, `affected_ids`, `result_id` (surviving id);
    on a real run also `operation_id`, `snapshot_id`, before/after preview (reversible).

    Example: `combine_paths(doc_id, ["a", "b"], dry_run=False, approval_token="ok")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: high (destructive geometry; approval-gated).
    """
    return _run_path_tool(
        doc_id, engine.COMBINE, object_ids, dry_run=dry_run, approval_token=approval_token
    )


@mcp.tool
def break_apart(
    doc_id: str,
    object_ids: list[str],
    dry_run: bool = True,
    approval_token: str | None = None,
) -> PathOpResult:
    """Break a compound path into its subpaths via the Inkscape engine (``path-break-apart``).

    When to use: splitting one compound path into separate paths. The inverse is `combine_paths`.

    Key params: `object_ids` are the compound paths to split. `dry_run=True` (DEFAULT) reports which
    ids would be broken apart WITHOUT invoking the engine; a real run needs a non-empty
    `approval_token`. Runs `select-by-id` + `path-break-apart`, writing back over the working copy.

    Return shape: `PathOpResult` — `dry_run`, `changed`, `affected_ids`; on a real run also
    `operation_id`, `snapshot_id`, before/after preview (reversible via the pre-op snapshot).

    Example: `break_apart(doc_id, ["glyph"], dry_run=False, approval_token="ok")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: high (destructive geometry; approval-gated).
    """
    return _run_path_tool(
        doc_id, engine.BREAK_APART, object_ids, dry_run=dry_run, approval_token=approval_token
    )


@mcp.tool
def stroke_to_path(
    doc_id: str,
    object_ids: list[str],
    dry_run: bool = True,
    approval_token: str | None = None,
) -> PathOpResult:
    """Convert each object's stroke into a filled path via the Inkscape engine.

    When to use: outlining a stroke into a fill so the visual stroke survives later scaling/export.
    To merely change stroke colour/width use `set_stroke` (non-destructive).

    Key params: `object_ids` are the targets. `dry_run=True` (DEFAULT) reports which ids would be
    outlined WITHOUT invoking the engine; a real run needs a non-empty `approval_token`. Runs
    `object-stroke-to-path` and writes back over the working copy.

    Return shape: `PathOpResult` — `dry_run`, `changed`, `affected_ids`; on a real run also
    `operation_id`, `snapshot_id`, before/after preview (reversible via the pre-op snapshot).

    Example: `stroke_to_path(doc_id, ["line"], dry_run=False, approval_token="ok")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: high (destructive geometry; approval-gated).
    """
    return _run_path_tool(
        doc_id, engine.STROKE_TO_PATH, object_ids, dry_run=dry_run, approval_token=approval_token
    )


@mcp.tool
def cleanup_paths(
    doc_id: str,
    object_ids: list[str],
    dry_run: bool = True,
    approval_token: str | None = None,
) -> PathOpResult:
    """Reduce path nodes via the Inkscape engine (``path-simplify``) — an explicit alias of
    `simplify_path`.

    When to use: same effect as `simplify_path`. For LOSSLESS cleanup (strip editor cruft, drop dead
    structure, round coordinates with references preserved) use `svg_web_optimize` — direct-DOM and
    non-destructive.

    Key params: `object_ids` are the targets. `dry_run=True` (DEFAULT) reports which ids would be
    simplified WITHOUT invoking the engine; a real run needs a non-empty `approval_token`. OVERLAP,
    DELIBERATE (P10): runs the SAME LOSSY `path-simplify` as `simplify_path` (removes nodes
    within a tolerance, can rewrite straight segments as curves). Inkscape 1.4.3 exposes no
    distinct, dependable non-destructive path-cleanup Action over `--actions`
    (`org.inkscape.path.to-absolute`
    is a no-op headless; `org.cutlings.clean-up-path` is a third-party extension we do not exec —
    sec.12), so this is kept as a named alias, not a different behavior.

    Return shape: `PathOpResult` — `dry_run`, `changed`, `affected_ids`; on a real run also
    `operation_id`, `snapshot_id`, before/after preview (reversible via the pre-op snapshot).

    Example: `cleanup_paths(doc_id, ["curve"], dry_run=False, approval_token="ok")`

    Render and look before you trust this edit: render with `render_preview` (or `live_render_view`)
    and inspect the result before relying on it; `restore_snapshot` reverts it if it is wrong.

    Risk class: high (destructive geometry, same as `simplify_path`; approval-gated).
    """
    return _run_path_tool(
        doc_id, engine.CLEANUP, object_ids, dry_run=dry_run, approval_token=approval_token
    )
