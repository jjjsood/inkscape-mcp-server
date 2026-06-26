"""Live view loop orchestrator (ADR-006 — ZERO new authority).

The flagship live-view step: it frames ONE perceive→decide→act→observe iteration as a single
bounded, cancelable unit by COMPOSING existing capabilities. It adds orchestration, not authority.

- PERCEIVE: capture the structured ``LiveScene`` (`get_live_scene`) + a canvas frame.
- DECIDE: the AGENT decides — this module embeds no LLM. The decision arrives as a TYPED, semantic
  act descriptor selecting ONE of the fixed write ops (``apply`` / ``insert_svg`` / ``set_text``)
  plus its validated params and an ``approval_token``. No act ⇒ a perceive-only step.
- ACT: routed through the write engines via :func:`run_live_mutation` — HIGH risk +
  ``approval_token`` + Live Operation Record + before/after frames. There is NO new mutation path,
  no raw Action, no code string (ADR-002/003): the step's mutation path IS the write path.
- OBSERVE: after an act, capture an after-scene + a focused ``live_diff_view`` linked to the
  Live Operation Record the act produced.

BOUNDED + CANCELABLE BY CONSTRUCTION: a single step is inherently one iteration — there is no
server-side autonomous loop. The agent drives the loop by calling the step repeatedly (and may use
``live_wait_for_change`` to react to user edits), so the loop is bounded by the agent's own calls
and cancelable at any point (the agent simply stops). This module exposes no multi-step runner.

SAFETY (sec.12): the act op is selected from a FIXED enum; every param is validated by the existing
validators (colour/length/number/text/SVG safe-parse) BEFORE it crosses the transport; the
approval gate is enforced solely by ``run_live_mutation`` (not reimplemented here). Perceive-only
steps mutate nothing and create no Operation Record. Errors are stable + host-path-free.
"""

from __future__ import annotations

import contextlib
from enum import StrEnum

from pydantic import BaseModel, Field

from inkscape_mcp.edit.dom import EditError
from inkscape_mcp.live.diff import LiveDiffResult, diff_live_operation
from inkscape_mcp.live.edit import (
    LiveEditResult,
    build_style,
    build_transform,
    run_live_mutation,
    validate_svg_fragment,
    validate_text,
)
from inkscape_mcp.live.protocol import LiveCommand
from inkscape_mcp.live.render import LiveRenderResult, render_live_view
from inkscape_mcp.live.scene import get_live_scene
from inkscape_mcp.live.transport import (
    BBox,
    LiveMutationResult,
    LiveScene,
    LiveTransport,
    SceneSelectionItem,
)
from inkscape_mcp.logging_setup import get_logger, log_tool_call

_logger = get_logger("live.loop")


class StepAction(StrEnum):
    """The FIXED set of semantic acts a loop step may perform (no raw Action/code path; ADR-003).

    Each value maps 1:1 to an existing semantic write engine; there is no escape hatch. An act
    descriptor naming anything outside this enum is rejected before any transport contact.
    """

    APPLY = "apply"
    INSERT_SVG = "insert_svg"
    SET_TEXT = "set_text"


class LiveSessionStepResult(BaseModel):
    """Outcome of one perceive→decide→act→observe iteration (`live_session_step`).

    Ties the iteration together: the BEFORE scene/frame always present (perceive); when an act ran,
    the ``operation_id`` of its Live Operation Record, the ``edit`` outcome, the focused ``diff``
    artifact (observe), and the AFTER scene/frame. On a perceive-only step ``acted`` is false and
    the act/observe fields are null.
    """

    acted: bool = Field(description="Whether a semantic act was performed this step.")
    action: StepAction | None = Field(
        default=None, description="The semantic act performed, or null on a perceive-only step."
    )
    before_scene: LiveScene = Field(description="Structured scene captured BEFORE deciding/acting.")
    before_frame: LiveRenderResult = Field(description="Canvas frame captured before acting.")
    operation_id: str | None = Field(
        default=None, description="Live Operation Record id of the act (null when perceive-only)."
    )
    edit: LiveEditResult | None = Field(
        default=None, description="The mutation outcome (null when perceive-only)."
    )
    diff: LiveDiffResult | None = Field(
        default=None, description="Focused before/after diff linked to the record (act steps only)."
    )
    after_scene: LiveScene | None = Field(
        default=None,
        description="Structured scene captured AFTER the act (null when perceive-only).",
    )
    after_frame: LiveRenderResult | None = Field(
        default=None, description="Canvas frame captured after the act (null when perceive-only)."
    )


def _perceive() -> tuple[LiveScene, LiveRenderResult]:
    """Capture the structured scene + a canvas frame (read-only; no Operation Record)."""
    frame = render_live_view()
    scene = get_live_scene()
    return scene, frame


def _build_act(
    action: StepAction,
    *,
    fill: str | None,
    stroke: str | None,
    stroke_width: str | None,
    opacity: float | None,
    dx: float | None,
    dy: float | None,
    scale: float | None,
    rotate: float | None,
    svg_fragment: str | None,
    text: str | None,
) -> tuple[str, LiveCommand, dict[str, object], object]:
    """Validate the act's params for the selected op and return its run_live_mutation wiring.

    Returns ``(tool_name, required_command, record_params, op)`` where ``op`` is the closure that
    invokes the SAME write engine the standalone live tool would. Raises a stable ``EditError``
    on any invalid/missing param. There is no branch that accepts a raw Action or code string.
    """
    if action is StepAction.APPLY:
        style = build_style(fill=fill, stroke=stroke, stroke_width=stroke_width, opacity=opacity)
        transform = build_transform(dx=dx, dy=dy, scale=scale, rotate=rotate)
        if not style and transform is None:
            raise EditError("apply requires at least one style or transform parameter")

        def op_apply(transport: LiveTransport) -> LiveMutationResult:
            return transport.apply_to_selection(style=style, transform=transform)

        return (
            "live_session_step:apply",
            LiveCommand.APPLY_TO_SELECTION,
            {"style": style, "transform": transform},
            op_apply,
        )

    if action is StepAction.INSERT_SVG:
        if svg_fragment is None:
            raise EditError("insert_svg requires an svg_fragment")
        fragment = validate_svg_fragment(svg_fragment)

        def op_insert(transport: LiveTransport) -> LiveMutationResult:
            return transport.insert_svg(fragment)

        return (
            "live_session_step:insert_svg",
            LiveCommand.INSERT_SVG,
            {"fragment_bytes": len(fragment.encode("utf-8"))},
            op_insert,
        )

    if action is StepAction.SET_TEXT:
        if text is None:
            raise EditError("set_text requires a text value")
        safe_text = validate_text(text)

        def op_text(transport: LiveTransport) -> LiveMutationResult:
            return transport.set_selected_text(safe_text)

        return (
            "live_session_step:set_text",
            LiveCommand.SET_SELECTED_TEXT,
            {"text_len": len(safe_text)},
            op_text,
        )

    # Unreachable for a valid StepAction; defends against an unmodeled enum extension.
    raise EditError(f"unknown step action: {action!r}")


def _observe(operation_id: str, after_scene: LiveScene) -> LiveDiffResult | None:
    """Capture the focused before/after diff for the act, linked to its record (best-effort).

    Reuses ``diff_live_operation`` against the SAME before/after frames the act already
    persisted on its Live Operation Record. Best-effort: a diff failure (e.g. a frame was
    unavailable) never invalidates a successful, recorded mutation.
    """
    selection: list[SceneSelectionItem] = after_scene.selection
    canvas: BBox | None = None
    if after_scene.canvas.width is not None and after_scene.canvas.height is not None:
        canvas = BBox(
            x=0.0, y=0.0, width=after_scene.canvas.width, height=after_scene.canvas.height
        )
    try:
        return diff_live_operation(operation_id, selection=selection, canvas=canvas)
    except Exception:  # pragma: no cover - diff is observe-only; never invalidates the mutation
        _logger.warning("loop observe diff unavailable", extra={"operation_id": operation_id})
        return None


def run_session_step(
    *,
    action: StepAction | None = None,
    approval_token: str | None = None,
    fill: str | None = None,
    stroke: str | None = None,
    stroke_width: str | None = None,
    opacity: float | None = None,
    dx: float | None = None,
    dy: float | None = None,
    scale: float | None = None,
    rotate: float | None = None,
    svg_fragment: str | None = None,
    text: str | None = None,
) -> LiveSessionStepResult:
    """Run ONE perceive→decide→act→observe iteration (the agent drives the loop).

    PERCEIVE always runs (read-only scene + frame). When ``action`` is None the step is
    perceive-only (no mutation, no Operation Record). When an ``action`` is supplied, the ACT is
    performed via the matching write engine through :func:`run_live_mutation` (HIGH +
    ``approval_token`` + Live Operation Record), then OBSERVE captures an after-scene + a focused
    diff linked to that record. Zero new authority: the mutation path is exactly the write path.
    """
    before_scene, before_frame = _perceive()

    if action is None:
        log_tool_call(
            _logger,
            tool="live_session_step",
            acted=False,
            selection=before_scene.selection_count,
            objects=before_scene.object_count,
        )
        return LiveSessionStepResult(
            acted=False, action=None, before_scene=before_scene, before_frame=before_frame
        )

    tool, required_command, params, op = _build_act(
        action,
        fill=fill,
        stroke=stroke,
        stroke_width=stroke_width,
        opacity=opacity,
        dx=dx,
        dy=dy,
        scale=scale,
        rotate=rotate,
        svg_fragment=svg_fragment,
        text=text,
    )
    # The approval gate + Live Operation Record + before/after capture all live in run_live_mutation
    # — not reimplemented here. A HIGH-risk act without approval_token is refused there.
    edit: LiveEditResult = run_live_mutation(
        tool=tool,
        params=params,
        required_command=required_command,
        op=op,  # type: ignore[arg-type]
        approval_token=approval_token,
    )

    # OBSERVE: capture the after-scene (best-effort) + the focused diff linked to the record.
    # Leave after_scene/after_frame as None if the post-act capture fails, so the caller can tell a
    # failed observe apart from "observed, nothing changed" (never silently echo the pre-act frame).
    after_scene: LiveScene | None = None
    after_frame: LiveRenderResult | None = None
    with contextlib.suppress(Exception):  # perception is feedback-only; never undoes the mutation
        after_scene, after_frame = _perceive()
    # The diff overlay needs selection/canvas metadata; use the post-act scene when captured, else
    # fall back to the pre-act scene for annotation only (the diff pixels come from the record).
    diff = _observe(edit.operation_id, after_scene if after_scene is not None else before_scene)

    log_tool_call(
        _logger,
        tool="live_session_step",
        acted=True,
        action=action.value,
        operation_id=edit.operation_id,
        affected=edit.count,
        diff=diff is not None,
    )
    return LiveSessionStepResult(
        acted=True,
        action=action,
        before_scene=before_scene,
        before_frame=before_frame,
        operation_id=edit.operation_id,
        edit=edit,
        diff=diff,
        after_scene=after_scene,
        after_frame=after_frame,
    )
