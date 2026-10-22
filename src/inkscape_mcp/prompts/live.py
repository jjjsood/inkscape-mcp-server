"""Live-view loop Prompt (architecture §4.1 — the `live_canvas_assist` entry point).

A Prompt orients the agent on HOW to run the live-view loop safely; it adds ZERO authority (ADR-006)
— it issues no command and grants no capability, it merely describes the perceive→decide→act→observe
loop and the rules around it. The actual work happens through the `live` and `live_edit` tools,
with every mutation still gated (HIGH risk + approval token).

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
        "Drive the canvas with ONE bounded perceive→decide→act→observe iteration at a time, with "
        "the `live` and `live_edit` tools. You — not the tools — decide each act; they embed no "
        "autonomy. The loop is bounded and cancelable BY CONSTRUCTION: every call is one step, you "
        "choose whether to make another, and you may stop at any point. There is no server-side "
        "autonomous run. Both tools connect automatically; `live(action='check')` shows what "
        "this host supports if they cannot.\n\n"
        "Loop:\n"
        "1. PERCEIVE — `live(action='observe', include=['selection', 'inspect', 'view'])` returns "
        "the selection (ids + bboxes), the selected objects' details and an inline PNG of the "
        "canvas in ONE call (`scene` adds the visible objects, `document` the document info). "
        "Reason over STRUCTURE, not pixels.\n"
        "2. DECIDE — pick ONE semantic act from the fixed set: `apply` (validated `style` and/or "
        "`transform` on the selection), `insert_svg` (a safe-parsed `svg_fragment`), or `set_text` "
        "(replace the selected text). There is NO free-text Action or code path.\n"
        "3. ACT — `live_edit(action=..., ..., observe='render', approval_token=...)`. Mutating a "
        "running session is HIGH risk: the act is REFUSED without an approval token. It produces a "
        "Live Operation Record, and `observe='render'` returns the canvas afterwards "
        "(`observe='diff'` a before/after overlay).\n"
        "4. OBSERVE — inspect the picture; `live(action='diff', operation_id=...)` shows exactly "
        "what an act changed. Then decide whether to iterate.\n\n"
        "Between iterations, use `live(action='wait')` to react to the user's own GUI edits "
        "instead of busy-looping; `live(action='viewport', ...)` moves the view. To work on the "
        "canvas as a tracked, snapshotted workspace document, `open(from_live={'dest_path': "
        "'...'})`. Never attempt to mutate without an approval token, and keep every act within "
        "the fixed set."
    )
