"""Live-view loop Prompt (E8-05, architecture §4.1 — the `live_canvas_assist` entry point).

A Prompt orients the agent on HOW to run the live-view loop safely; it adds ZERO authority (ADR-006)
— it issues no command and grants no capability, it merely describes the perceive→decide→act→observe
loop and the rules around it. The actual work happens through the existing typed tools, with every
mutation still gated by `run_live_mutation` (HIGH + approval).

Registered via ``@mcp.prompt`` against the shared app; the module import (wired in
``server.register_tools``) runs the decorator and self-registers the prompt.
"""

from __future__ import annotations

from inkscape_mcp.server import mcp


@mcp.prompt
def live_canvas_assist(goal: str) -> str:
    """Entry point for the live-view co-pilot loop: perceive→decide→act→observe (approval-gated).

    Orients the agent to drive a running Inkscape via the live tools toward `goal`, one bounded,
    cancelable iteration at a time. Acts are semantic-only and approval-gated; the loop adds no new
    authority.
    """
    # Collapse newlines + bound the length so a crafted goal cannot break out of the single
    # instruction line (the prompt grants no authority, but keep the structure intact).
    clean_goal = " ".join(goal.split())[:500] or "(no goal provided)"
    return (
        "You are a live-canvas co-pilot for a RUNNING Inkscape instance. Work toward this goal:\n"
        f"  {clean_goal}\n\n"
        "Drive the canvas with ONE bounded perceive→decide→act→observe iteration at a time, "
        "using `live_session_step`. You — not the tool — decide each act; the tool embeds no "
        "autonomy. The loop is bounded and cancelable BY CONSTRUCTION: each `live_session_step` "
        "is exactly one iteration, you choose whether to call it again, and you may stop at any "
        "point. There is no server-side autonomous run.\n\n"
        "Loop:\n"
        "1. PERCEIVE — call `live_session_step` with NO action first to read the structured "
        "`LiveScene` (selection ids + bboxes, viewport, canvas, visible objects) and a frame. "
        "Reason over STRUCTURE, not pixels.\n"
        "2. DECIDE — pick ONE semantic act from the fixed set: `apply` (validated style and/or "
        "transform on the selection), `insert_svg` (a safe-parsed SVG fragment), or `set_text` "
        "(replace the selected text). There is NO raw-Action or code path.\n"
        "3. ACT — call `live_session_step` again with that `action`, its validated params, and "
        "an `approval_token`. Mutating a running session is HIGH risk: the act is REFUSED without "
        "an approval token. Every act flows through the same governed write path as the standalone "
        "live write tools, producing a Live Operation Record with before/after frames.\n"
        "4. OBSERVE — the step returns the after-scene plus a focused before/after "
        "`live_diff_view` linked to the operation. Inspect it, then decide whether to iterate.\n\n"
        "Between iterations, use `live_wait_for_change` to react to the user's own GUI edits "
        "instead of busy-looping. Connect with `live_connect` first; sync results into the "
        "workspace with `live_sync_to_workspace` when you want a tracked, snapshotted copy. Never "
        "attempt to mutate without an approval token, and keep every act within the fixed set."
    )
