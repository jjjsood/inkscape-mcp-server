"""Controlled Action chains (E6-02): typed steps → validation → arg-list argv → engine.

An Action chain is an ORDERED LIST OF TYPED STEPS (:class:`ActionStep`), never a raw string. Each
step names ONE Inkscape Action plus an optional list of already-typed argument tokens. Before a
chain can run it is validated against TWO independent gates:

1. the server-side allowlist (`Settings.action_allowlist` — never client-supplied), and
2. the version-keyed capability map (the Action must actually exist on THIS Inkscape).

Validation produces a :class:`ActionChainPlan` with the EXACT resolved ``--actions`` argument and
the normalized step list. A dry run returns that plan WITHOUT invoking Inkscape — the auditable
preview of what would run. A real run assembles the same argv (arg-lists only, ``shell=False``) and
executes it through the Inkscape engine over a document's working copy, routed through the E2-04
mutating pipeline so the change is snapshotted, recorded (HIGH-risk + approval-gated), reversible.

There is NO open-string Action passthrough here — every token is allowlisted AND present in the map
AND charset-checked before it hits argv (ADR-003; no arbitrary extension exec; sec.12).
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from lxml import etree
from pydantic import BaseModel, Field

from inkscape_mcp.actions.capability_map import ActionCapabilityMap, get_or_build_action_map
from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.engine.ops import engine_mode_is_shell, engine_run_actions
from inkscape_mcp.engine.process import EngineError
from inkscape_mcp.logging_setup import get_logger
from inkscape_mcp.workspace.limits import LimitExceeded, check_output_size
from inkscape_mcp.workspace.subprocess_exec import ProcessError, ProcessResult, run_inkscape
from inkscape_mcp.workspace.xml_safety import UnsafeXMLError, parse_svg_bytes

logger = get_logger("actions.chains")

#: An Action id is conservatively constrained to the charset Inkscape uses (letters, digits, and
#: ``. - _`` plus a namespaced ``:``). The ``:`` is ALSO the ``--actions`` verb/argument delimiter,
#: so a namespaced action id is permitted only as the WHOLE token; an embedded ``:`` inside an
#: ARGUMENT is rejected separately (see `_SAFE_ARG_RE`) so a crafted arg can never inject a verb.
#: At most ONE ``:`` is allowed (a single optional namespace prefix); ``ns::a``/``a:b:c`` are
#: rejected so the token can never carry the action-grammar delimiter more than the grammar permits.
_SAFE_ACTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(:[A-Za-z0-9][A-Za-z0-9._-]*)?$")

#: An Action argument token: no ``:`` (verb/arg delimiter), no ``;`` (step delimiter), no ``,`` (the
#: select-by-id multi-id delimiter), no whitespace, no shell/argv-hostile chars. This is the only
#: place a per-step value is composed into argv, so the charset is deliberately tight.
_SAFE_ARG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._#-]*$")

#: Hard cap on chain length — a chain is a small, deliberate composition, not a batch program. Keeps
#: the assembled ``--actions`` element bounded and the audit record readable.
MAX_CHAIN_STEPS = 32

#: Hard cap on arguments per step.
MAX_STEP_ARGS = 16


class ActionChainError(Exception):
    """An Action chain is invalid or refused.

    Carries a stable, machine-readable, host-path-free message plus a `code` token so callers can
    branch on the failure class without string-matching. The tool layer maps this to a `ToolError`.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ActionEngineError(Exception):
    """The Inkscape engine failed, timed out, or produced no/unsafe output running a chain."""


class ActionStep(BaseModel):
    """One typed step in a controlled Action chain.

    `action` is a single Inkscape Action id (validated against the allowlist + capability map).
    `args` is an ordered list of already-typed argument tokens passed to that Action; each is
    charset-checked. To select MULTIPLE ids with ``select-by-id``, pass each id as its OWN token
    (e.g. ``args=["p_a", "p_b"]``) — NOT one comma-joined string (``args=["p_a,p_b"]`` is rejected,
    because a comma is a reserved argv delimiter); the engine joins the tokens with commas itself.
    There is no free-form string field — the step IS the structure.
    """

    action: str
    args: list[str] = Field(default_factory=list)


class ActionChainPlan(BaseModel):
    """The validated, resolved plan for an Action chain (the dry-run preview + the run record).

    `steps` is the normalized step list; `actions_argument` is the EXACT string assembled into the
    single ``--actions`` argv element; `argv_preview` is the full Inkscape argument list that WOULD
    run (working-copy path shown as a placeholder for a pure-validation dry run). `valid` is always
    True for a returned plan (an invalid chain raises `ActionChainError` instead).
    """

    steps: list[ActionStep]
    actions_argument: str
    argv_preview: list[str]
    inkscape_version: str | None = None
    valid: bool = True


def _validate_step(
    step: ActionStep, allowlist: frozenset[str], cap_map: ActionCapabilityMap
) -> ActionStep:
    """Validate one step and return its NORMALIZED form (the exact tokens that will hit argv).

    Raises `ActionChainError` with a machine-readable `code` for: a malformed/empty action token
    (`malformed_action`), an action not on the server-side allowlist (`not_allowlisted`), an action
    absent from THIS Inkscape's capability map (`action_absent`), too many args (`too_many_args`),
    or a malformed argument token (`malformed_arg`). Returns the step with the action and args
    stripped, so the assembled argv can only ever contain the validated tokens (no whitespace can
    survive into the ``--actions`` element). Every check runs BEFORE any argv assembly.
    """
    action = step.action.strip()
    if not action or not _SAFE_ACTION_RE.match(action):
        raise ActionChainError("malformed_action", f"malformed action id: {action!r}")
    if action not in allowlist:
        raise ActionChainError(
            "not_allowlisted", f"action {action!r} is not allowlisted for execution"
        )
    if not cap_map.has(action):
        raise ActionChainError(
            "action_absent",
            f"action {action!r} is not available on Inkscape {cap_map.inkscape_version}",
        )
    if len(step.args) > MAX_STEP_ARGS:
        raise ActionChainError("too_many_args", f"action {action!r} has too many arguments")
    norm_args: list[str] = []
    for arg in step.args:
        token = arg.strip()
        if not token or not _SAFE_ARG_RE.match(token):
            raise ActionChainError("malformed_arg", _malformed_arg_message(arg))
        norm_args.append(token)
    return ActionStep(action=action, args=norm_args)


def _malformed_arg_message(arg: str) -> str:
    """Build the `malformed_arg` message, with an actionable hint for comma-joined ids.

    The single biggest foot-gun (E11-10(e) / S15) is passing a comma-joined multi-id token like
    ``"p_a,p_b"`` as ONE arg — a comma is a reserved argv delimiter, so the token is rejected. The
    correct multi-id form is SEPARATE tokens in `args` (e.g. ``args=["p_a", "p_b"]``); the engine
    joins them with commas itself. Detect that exact case and steer the caller to the right shape.
    """
    base = f"malformed argument token: {arg!r}"
    if "," in arg:
        ids = ", ".join(repr(part) for part in arg.split(",") if part)
        return (
            f"{base}. HINT: pass each id as a SEPARATE arg token, not a comma-joined string "
            f"(e.g. args=[{ids}] for select-by-id); the engine joins them with commas itself."
        )
    return base


def _assemble_actions_argument(steps: list[ActionStep]) -> str:
    """Assemble the single ``--actions`` value from validated steps (the ONLY composition point).

    Each step becomes ``action`` or ``action:arg1,arg2`` (Inkscape's verb/argument grammar); steps
    are joined with ``;``. Every token is already allowlist- and charset-validated, so no untrusted
    string is interpolated. No shell string is ever built — this value goes into one argv element.
    """
    parts: list[str] = []
    for step in steps:
        if step.args:
            parts.append(f"{step.action}:{','.join(step.args)}")
        else:
            parts.append(step.action)
    return ";".join(parts)


def validate_chain(
    steps: list[ActionStep],
    *,
    settings: Settings | None = None,
    cap_map: ActionCapabilityMap | None = None,
    working_path_placeholder: str = "<working-copy>",
) -> ActionChainPlan:
    """Validate an ordered Action chain and return its resolved plan (NO engine invocation).

    Validates length, then each step against the server-side allowlist AND the version capability
    map AND the safe charsets, then assembles the exact ``--actions`` argument and the full argv
    preview. This is the shared gate for dry-run AND real execution: a real run re-runs the same
    validation before invoking Inkscape. Raises `ActionChainError` (machine-readable `code`) on the
    first invalid step; the caller never reaches argv assembly for a bad chain.
    """
    s = settings if settings is not None else get_settings()
    cmap = cap_map if cap_map is not None else get_or_build_action_map(settings=s)

    if not steps:
        raise ActionChainError("empty_chain", "action chain has no steps")
    if len(steps) > MAX_CHAIN_STEPS:
        raise ActionChainError("chain_too_long", f"action chain exceeds {MAX_CHAIN_STEPS} steps")

    normalized = [_validate_step(step, s.action_allowlist, cmap) for step in steps]

    actions_argument = _assemble_actions_argument(normalized)
    # Build the preview from the SAME argv builder a real run uses, with placeholder in/out paths,
    # so the plan's argv_preview matches the shape that would actually be invoked (auditability).
    argv_preview = _build_engine_args(
        Path(working_path_placeholder), Path("<export-output>"), actions_argument
    )
    return ActionChainPlan(
        steps=normalized,
        actions_argument=actions_argument,
        argv_preview=argv_preview,
        inkscape_version=cmap.inkscape_version,
    )


def _build_engine_args(working_path: Path, out_path: Path, actions_argument: str) -> list[str]:
    """Build the Inkscape argv for a chain run (arg list only — sec.12).

    The single ``--actions`` element is the validated, pre-assembled value; the in/out paths are
    server-controlled (the registry working copy and a server-created temp file). The chain is run
    as an SVG export so the engine emits the mutated document to the temp file. No shell string.
    """
    return [
        str(working_path),
        f"--actions={actions_argument}",
        "--export-type=svg",
        "--export-plain-svg",
        f"--export-filename={out_path}",
    ]


def run_chain_op(
    tree: etree._ElementTree,
    working_path: Path,
    steps: list[ActionStep],
    *,
    settings: Settings | None = None,
    cap_map: ActionCapabilityMap | None = None,
) -> str:
    """Pipeline `mutate` kernel: validate + run a chain on the engine, replace the tree in memory.

    Used as the ``mutate(tree) -> str`` callback for :func:`inkscape_mcp.edit.pipeline.apply_edit`.
    RE-VALIDATES the chain (allowlist + capability map + charset) before any invocation, runs the
    assembled ``--actions`` argv through Inkscape over the on-disk working copy, parses the engine's
    SVG output through the SAFE parser, and replaces the working tree's root so the pipeline
    persists it. Raises `ActionChainError` on an invalid chain (pipeline discards, nothing written)
    and `ActionEngineError` on an engine failure/timeout/empty/oversized/unsafe output.
    """
    s = settings if settings is not None else get_settings()
    plan = validate_chain(steps, settings=s, cap_map=cap_map)
    data = _run_engine(working_path, plan.actions_argument, s)
    try:
        new_root = parse_svg_bytes(data).getroot()
    except UnsafeXMLError as exc:  # pragma: no cover - _run_engine already rejected unsafe output
        raise ActionEngineError("action chain produced unsafe output") from exc
    tree._setroot(new_root)
    return f"applied action chain ({len(plan.steps)} step(s)): {plan.actions_argument}"


def _run_engine(working_path: Path, actions_argument: str, settings: Settings) -> bytes:
    """Run the assembled chain through Inkscape and return the mutated, safe-parsed SVG bytes.

    The working copy is NOT modified here — Inkscape emits to a private temp file that is always
    removed; the bytes are returned for the pipeline to persist. Output is size-capped and re-parsed
    through the safe parser. A non-zero exit, timeout, missing/empty/oversized/unsafe output raises
    `ActionEngineError`.
    """
    # E12-04: route the validated chain through the warm shell worker when enabled; ANY engine fault
    # falls back to the per-call CLI below so correctness can never regress.
    if engine_mode_is_shell(settings):
        try:
            return engine_run_actions(working_path, actions_argument, settings=settings)
        except EngineError as exc:
            logger.warning(
                "warm engine action chain fell back to per-call CLI",
                extra={"error": type(exc).__name__},
            )

    fd, tmp_name = tempfile.mkstemp(prefix="inkscape-mcp-chain-", suffix=".svg")
    out_path = Path(tmp_name)
    try:
        os.close(fd)
        args = _build_engine_args(working_path, out_path, actions_argument)
        try:
            result: ProcessResult = run_inkscape(args, settings=settings)
        except ProcessError as exc:
            raise ActionEngineError("inkscape engine unavailable") from exc

        if result.timed_out:
            logger.error("action chain timed out", extra={"duration_s": result.duration_s})
            raise ActionEngineError("action chain timed out")
        if result.returncode != 0 or not out_path.exists():
            logger.error("action chain failed", extra={"returncode": result.returncode})
            raise ActionEngineError("action chain failed")

        try:
            check_output_size(out_path, settings)
        except LimitExceeded as exc:
            raise ActionEngineError("action chain output exceeds size limit") from exc

        data = out_path.read_bytes()
        if not data.strip():
            raise ActionEngineError("action chain produced empty output")
        try:
            parse_svg_bytes(data)
        except UnsafeXMLError as exc:
            raise ActionEngineError("action chain produced unsafe output") from exc
        return data
    finally:
        try:
            out_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best-effort cleanup
            pass
