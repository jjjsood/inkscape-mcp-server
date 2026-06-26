"""Controlled Action/Extension tools (E6-02): discovery (low) + chain validation/execution (gated).

A thin `@mcp.tool` layer over :mod:`inkscape_mcp.actions`. Four small typed tools (ADR-002, no
portmanteau):

- ``list_actions`` — read-only discovery of the host's probed Action surface + the server-side
  allowlisted/available subsets. Also persists the version-keyed capability map as a side effect.
- ``discover_extensions`` — read-only diagnostic listing of the allowlisted extension set + probe
  notes. No extension executes from here.
- ``validate_action_chain`` — DRY-RUN: validate an ordered list of typed Action steps against the
  allowlist AND the version map AND the safe charsets, returning the EXACT resolved ``--actions``
  argument + argv preview WITHOUT invoking Inkscape. Invalid chains are refused with a
  machine-readable error code.
- ``run_action_chain`` — HIGH risk + approval-gated execution of a validated chain over a
  document's working copy, routed through the E2-04 mutating pipeline (snapshot + Operation Record +
  before/after preview, reversible). Refuses anything not allowlisted-and-present, and refuses
  outright without an `approval_token`.
- ``run_raw_action`` — the optional ADR-003 escape hatch (E6-03): a single, advanced-mode-only,
  OFF-by-default, HIGH-risk raw-Action runner. It is a thin façade over the SAME chain machinery
  (one typed Action + typed args → a one-step chain), adding only the `raw_action_enabled` gate and
  the single-Action ergonomics. NOT a portmanteau replacement for the semantic tools (ADR-002) and
  NOT an open-string passthrough: it accepts a typed `action` + `args` (never a free-form
  ``--actions`` string), validates against the allowlist AND the version map AND the safe charsets,
  defaults to dry-run, and on a real run requires an `approval_token` and routes through the same
  mutating pipeline (snapshot + Operation Record + before/after preview, reversible).

This is the gate machinery E6-03 (the raw-action escape hatch) reuses — there is no open-string
passthrough here (ADR-003; sec.12 / X1).
"""

from __future__ import annotations

from pathlib import Path

from fastmcp.exceptions import ToolError
from lxml import etree
from pydantic import BaseModel

from inkscape_mcp.actions import chains as chain_engine
from inkscape_mcp.actions.capability_map import (
    ActionDiscovery,
    ExtensionDiscovery,
    get_or_build_action_map,
    persist_action_map,
)
from inkscape_mcp.actions.capability_map import (
    discover_actions as _discover_actions,
)
from inkscape_mcp.actions.capability_map import (
    discover_extensions as _discover_extensions,
)
from inkscape_mcp.actions.chains import (
    ActionChainError,
    ActionChainPlan,
    ActionEngineError,
    ActionStep,
)
from inkscape_mcp.config import get_settings
from inkscape_mcp.document.inspect import DocumentNotFound, InspectionError
from inkscape_mcp.edit.pipeline import EditApplyError, apply_edit
from inkscape_mcp.logging_setup import get_logger, log_tool_call
from inkscape_mcp.registry import DocEntry, get_registry
from inkscape_mcp.server import mcp
from inkscape_mcp.workspace.risk import PolicyViolation, RiskClass

_logger = get_logger("tools.actions")

#: Stable engine message (from `inkscape_mcp.actions.chains`) signalling the Inkscape engine could
#: not be launched on this runtime (capability ABSENT). Matched at the tool layer so the client-
#: facing error can NAME the discovery tool without the engine importing the tool surface (E14-08a).
_ENGINE_UNAVAILABLE = "inkscape engine unavailable"


def _chain_error_message(exc: ActionChainError) -> str:
    """Stable, machine-readable message for an `ActionChainError` (E14-08a).

    The `action_absent` code means the requested Action is not present on THIS Inkscape runtime (a
    capability ABSENCE), so the message NAMES `list_actions` so the agent can see which Actions this
    runtime actually exposes; every other chain error (not_allowlisted, malformed_*, empty_chain,
    chain_too_long, too_many_args) keeps its own already-safe `code: message`.
    """
    if exc.code == "action_absent":
        return (
            f"{exc.code}: {exc.message}; "
            "call list_actions to see which Actions this runtime exposes"
        )
    return f"{exc.code}: {exc.message}"


def _engine_error_message(exc: ActionEngineError) -> str:
    """Stable, host-path-free message for an `ActionEngineError` (E14-08a).

    When the engine is ABSENT (no Inkscape binary on this runtime) the message names the discovery
    tool so the agent can inspect support rather than retry blindly; every other engine error
    (timeout, failed chain, unsafe output) keeps its own already-safe message.
    """
    if str(exc) == _ENGINE_UNAVAILABLE:
        return (
            "inkscape engine unavailable on this runtime; "
            "call list_capabilities to see what this runtime supports"
        )
    return str(exc)


class ActionChainResult(BaseModel):
    """Outcome of `run_action_chain` (always a real, approved run).

    `changed` is True on success; `operation_id`, `snapshot_id`, and the before/after preview paths
    (workspace-relative, or `None` when a render was unavailable) link the recorded, reversible
    operation. `plan` echoes the validated chain that ran (its resolved ``--actions`` argument).
    """

    doc_id: str
    changed: bool
    plan: ActionChainPlan
    summary: str | None = None
    operation_id: str | None = None
    snapshot_id: str | None = None
    preview_before: str | None = None
    preview_after: str | None = None


@mcp.tool
def list_actions(include_all_actions: bool = True) -> ActionDiscovery:
    """Discover the host's actual Inkscape Action surface (probe-driven, version-aware).

    When to use: to see which Actions a chain step may use before `validate_action_chain` /
    `run_action_chain`. For allowlisted extensions use `discover_extensions`; for the full runtime
    matrix use `list_capabilities`.

    Key params: `include_all_actions` (default True for back-compat) controls the large `actions`
    array — the host reports ~1000 ids but only `allowlisted`/`available` can ever execute, so pass
    `False` to OMIT `actions` while keeping the accurate `action_count` (a much smaller payload,
    E13-05).

    Return shape: `ActionDiscovery` — `actions` (all host ids unless omitted), `action_count`,
    `allowlisted` (may ever execute), `available` (allowlisted AND present), `notes`. Side effect:
    persists the version-keyed Action capability map for later chain validation. A missing Inkscape
    degrades to empty lists plus `notes`.

    Example: `list_actions(include_all_actions=False)`

    Risk class: low (read-only discovery).
    """
    discovery = _discover_actions()
    if not include_all_actions:
        # Keep `action_count` truthful (the real host total); just drop the bulky id array.
        discovery = discovery.model_copy(update={"actions": []})
    # Persist the version-keyed capability map as a side effect of discovery (best-effort).
    try:
        persist_action_map(get_or_build_action_map())
    except Exception:  # pragma: no cover - persistence is best-effort, never fails discovery
        _logger.warning("action map persistence failed", extra={"event": "file_io"})
    log_tool_call(
        _logger,
        tool="list_actions",
        action_count=discovery.action_count,
        available=len(discovery.available),
        risk_class=RiskClass.LOW.value,
    )
    return discovery


@mcp.tool
def discover_extensions() -> ExtensionDiscovery:
    """List the server-side allowlisted Inkscape extensions + probe notes (read-only, diagnostic).

    When to use: to see which extensions (if any) an operator has opted in. For the Action surface
    use `list_actions`; for the full runtime matrix use `list_capabilities`.

    Key params: none.

    Return shape: `ExtensionDiscovery` — `allowlisted` (the opted-in set; empty by default in the
    MVP) plus probe `notes`. No extension runs from this call.

    Example: `discover_extensions()`

    Risk class: low (read-only).
    """
    discovery = _discover_extensions()
    log_tool_call(
        _logger,
        tool="discover_extensions",
        allowlisted=len(discovery.allowlisted),
        risk_class=RiskClass.LOW.value,
    )
    return discovery


@mcp.tool
def validate_action_chain(steps: list[ActionStep]) -> ActionChainPlan:
    """Dry-run-validate an ordered Action chain and return its resolved plan (NO mutation).

    When to use: to preview the resolved argv of a chain before `run_action_chain` executes it. For
    a semantic edit prefer the dedicated typed tool (`find_objects` → set/transform/path tools).

    Key params: `steps` is the ordered list of typed Action steps. To select MULTIPLE ids with
    `select-by-id`, pass each id as its OWN token (e.g. `args=["p_a", "p_b"]`), NOT one comma-joined
    string — a comma-joined token is rejected with `malformed_arg` (the engine joins with commas
    itself).

    Return shape: `ActionChainPlan` — the validated `steps` plus the EXACT `--actions` argument and
    argv preview a real run WOULD assemble. An invalid chain raises a `ToolError` whose code names
    the cause (not_allowlisted, action_absent, malformed_action, malformed_arg, empty_chain,
    chain_too_long, too_many_args).

    Example: `validate_action_chain([{"action": "select-by-id", "args": ["p1"]}])`

    Risk class: low (validation only — no engine invocation, no write).
    """
    try:
        plan = chain_engine.validate_chain(steps)
    except ActionChainError as exc:
        raise ToolError(_chain_error_message(exc)) from exc
    log_tool_call(
        _logger,
        tool="validate_action_chain",
        steps=len(plan.steps),
        dry_run=True,
        risk_class=RiskClass.LOW.value,
    )
    return plan


@mcp.tool
def run_action_chain(
    doc_id: str,
    steps: list[ActionStep],
    approval_token: str | None = None,
) -> ActionChainResult:
    """Execute a validated Action chain over a document's working copy (HIGH risk, approval-gated).

    When to use: when a semantic typed tool cannot express the edit. Run `validate_action_chain`
    first to preview the resolved argv. For a single Action use `run_raw_action` (advanced mode).

    Key params: `steps` is the ordered typed chain, RE-validated against the allowlist + capability
    map + charset before any invocation (anything not allowlisted-and-present is refused). A real
    run REQUIRES a non-empty `approval_token`. To select MULTIPLE ids with `select-by-id`, pass
    each id as its OWN token (e.g. `args=["p_a", "p_b"]`), NOT one comma-joined string (comma is a
    reserved argv delimiter; the engine joins with commas itself).

    Return shape: `ActionChainResult` — `changed`, `plan` (the validated chain that ran),
    `operation_id`, `snapshot_id`, before/after preview. Execution runs the assembled `--actions`
    argv (arg-lists only, `shell=False`) through the pipeline (reversible).

    Example: `run_action_chain(doc_id, steps, approval_token="ok")`

    Risk class: high — controlled Action execution. Requires `approval_token`.
    """
    if not approval_token:
        raise ToolError("high-risk action chain requires an explicit approval_token")

    # Validate up front so an invalid chain fails with a chain-shaped, machine-readable error
    # BEFORE the pipeline opens a record (the mutate closure re-validates as defence in depth).
    try:
        plan = chain_engine.validate_chain(steps)
    except ActionChainError as exc:
        raise ToolError(_chain_error_message(exc)) from exc

    def mutate(tree: etree._ElementTree) -> str:
        entry: DocEntry = get_registry().get(doc_id)
        return chain_engine.run_chain_op(tree, Path(entry.working_path), steps)

    try:
        result = apply_edit(
            doc_id,
            "run_action_chain",
            {"steps": [s.model_dump() for s in steps], "actions": plan.actions_argument},
            mutate,
            approval_token=approval_token,
            risk_class=RiskClass.HIGH,
        )
    except (EditApplyError, DocumentNotFound, KeyError) as exc:
        raise ToolError("document id not found") from exc
    except PolicyViolation as exc:
        raise ToolError(str(exc)) from exc
    except ActionChainError as exc:
        raise ToolError(_chain_error_message(exc)) from exc
    except ActionEngineError as exc:
        raise ToolError(_engine_error_message(exc)) from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc

    log_tool_call(
        _logger,
        tool="run_action_chain",
        doc_id=doc_id,
        operation_id=result.operation_id,
        snapshot_id=result.snapshot_id,
        steps=len(plan.steps),
        risk_class=RiskClass.HIGH.value,
    )
    return ActionChainResult(
        doc_id=doc_id,
        changed=result.changed,
        plan=plan,
        summary=result.summary,
        operation_id=result.operation_id,
        snapshot_id=result.snapshot_id,
        preview_before=result.preview_before,
        preview_after=result.preview_after,
    )


class RawActionResult(BaseModel):
    """Outcome of `run_raw_action` (the ADR-003 escape hatch).

    `dry_run` reflects whether this was a preview (no engine invocation, no mutation) or a real run.
    `plan` is the validated one-step chain (its resolved ``--actions`` argument + argv preview).
    `changed` is True only on an applied run; `operation_id`, `snapshot_id`, and the before/after
    preview paths (workspace-relative, or `None`) link the recorded, reversible operation — all
    `None` on a dry run.
    """

    doc_id: str
    dry_run: bool
    changed: bool
    plan: ActionChainPlan
    summary: str | None = None
    operation_id: str | None = None
    snapshot_id: str | None = None
    preview_before: str | None = None
    preview_after: str | None = None


@mcp.tool
def run_raw_action(
    doc_id: str,
    action: str,
    args: list[str] | None = None,
    dry_run: bool = True,
    approval_token: str | None = None,
) -> RawActionResult:
    """Run ONE raw, allowlisted Inkscape Action over a document (advanced mode; OFF by default).

    When to use: the explicit ADR-003 escape hatch for a SINGLE Action no semantic tool covers. For
    several Actions use `run_action_chain`; prefer the typed semantic tools (ADR-002) when possible.

    Key params: pass a typed `action` id plus typed `args` (never a free-form `--actions` string).
    It is composed into a one-step chain and runs through the SAME validation + engine as
    `run_action_chain` (allowlist-, version-map-, charset-gated; arg-lists only, `shell=False`). The
    tool is refused entirely unless `INKSCAPE_MCP_RAW_ACTION_ENABLED` is set (enabling it does not
    widen the allowlist). `dry_run=True` (DEFAULT) returns the resolved plan WITHOUT invoking
    Inkscape; a real run REQUIRES a non-empty `approval_token`.

    Return shape: `RawActionResult` — `dry_run`, `changed`, `plan` (resolved one-step chain); on a
    real run also `operation_id`, `snapshot_id`, before/after preview (reversible).

    Example: `run_raw_action(doc_id, "path-union", dry_run=False, approval_token="ok")`

    Risk class: high — controlled raw Action execution (advanced mode). Requires `approval_token`
    for a non-dry run.
    """
    if not get_settings().raw_action_enabled:
        raise ToolError(
            "raw_action_disabled: raw-action tool is disabled "
            "(set INKSCAPE_MCP_RAW_ACTION_ENABLED to enable advanced mode)"
        )

    # Validate the target document up front for BOTH paths: a dry-run must not report success for a
    # fabricated doc_id, and an unapproved real run is refused before any capability-map probe.
    try:
        get_registry().get(doc_id)
    except KeyError as exc:
        raise ToolError("document id not found") from exc
    if not dry_run and not approval_token:
        raise ToolError("high-risk raw action requires an explicit approval_token")

    steps = [ActionStep(action=action, args=list(args or []))]

    # Validate (allowlist + capability map + charset) up front so an absent/invalid Action fails
    # with a chain-shaped, machine-readable error BEFORE a dry-run plan or any pipeline record.
    try:
        plan = chain_engine.validate_chain(steps)
    except ActionChainError as exc:
        raise ToolError(_chain_error_message(exc)) from exc

    if dry_run:
        log_tool_call(
            _logger,
            tool="run_raw_action",
            doc_id=doc_id,
            action=plan.steps[0].action,
            dry_run=True,
            risk_class=RiskClass.HIGH.value,
        )
        return RawActionResult(doc_id=doc_id, dry_run=True, changed=False, plan=plan)

    def mutate(tree: etree._ElementTree) -> str:
        entry: DocEntry = get_registry().get(doc_id)
        return chain_engine.run_chain_op(tree, Path(entry.working_path), steps)

    try:
        result = apply_edit(
            doc_id,
            "run_raw_action",
            {
                "action": plan.steps[0].action,
                "args": plan.steps[0].args,
                "actions": plan.actions_argument,
            },
            mutate,
            approval_token=approval_token,
            risk_class=RiskClass.HIGH,
        )
    except (EditApplyError, DocumentNotFound, KeyError) as exc:
        raise ToolError("document id not found") from exc
    except PolicyViolation as exc:
        raise ToolError(str(exc)) from exc
    except ActionChainError as exc:
        raise ToolError(_chain_error_message(exc)) from exc
    except ActionEngineError as exc:
        raise ToolError(_engine_error_message(exc)) from exc
    except InspectionError as exc:
        raise ToolError("document could not be parsed safely") from exc

    log_tool_call(
        _logger,
        tool="run_raw_action",
        doc_id=doc_id,
        operation_id=result.operation_id,
        snapshot_id=result.snapshot_id,
        action=plan.steps[0].action,
        dry_run=False,
        risk_class=RiskClass.HIGH.value,
    )
    return RawActionResult(
        doc_id=doc_id,
        dry_run=False,
        changed=result.changed,
        plan=plan,
        summary=result.summary,
        operation_id=result.operation_id,
        snapshot_id=result.snapshot_id,
        preview_before=result.preview_before,
        preview_after=result.preview_after,
    )
