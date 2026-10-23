"""In-context system overview delivered as MCP server ``instructions``.

The model should hold the document model + idioms every turn, not only if it happens to open a
``docs/`` file, so a CONCISE orientation is delivered in-context via the FastMCP ``instructions``
field.

It is deliberately a SHORT orientation + pointers, NOT a second copy of the agent usage guide
(``docs/agent-usage-guide.md``): it states the load-bearing invariants (the ``open`` ->
``edit(..., render='inline')`` -> ``export`` flow and the ``doc_id`` lifecycle, ops with ``$n``
references and selectors, the snapshot/restore reversibility idiom, the risk classes +
``approval_token`` gate, and the render-and-look default) and then routes to the discovery surface
(``help`` / ``inkscape://ops`` / ``llms.txt``) for everything else. The deep detail stays
single-sourced in the guide and the generated manifest.
"""

from __future__ import annotations

#: The concise, always-in-context overview handed to FastMCP as ``instructions``. Kept short on
#: purpose — it orients, then points at the discovery surface for specifics.
SYSTEM_OVERVIEW = """\
inkscape-mcp makes Inkscape/SVG documents agent-ready through ten result-oriented tools (not a
free-text run_action / execute_code portmanteau). Orientation:

Flow. `open` (a workspace file, SVG text, a blank canvas, or the live canvas) returns a `doc_id`
and can already apply the first `ops`. `edit(doc_id, ops=[...], render='inline')` applies MANY typed
ops in ONE call and returns the ids it created plus a picture of the result. `export(doc_id,
targets=[...])` writes png/pdf/svg files, profiles (web/icons/print) or save_as. All writes land on
a WORKING COPY; the original source file is never modified.

Ops. An op is `{op: 'create_rect', x, y, width, height}`. `"$<n>"` names the id that op n of the
same call created, so a whole drawing is one call; ops that act on objects take `ids` or a `select`
({fill, stroke, tag, text, id_prefix, bbox}) instead of a separate lookup. Ops run in
order, each seeing the document as the previous ops left it. A call is atomic: one bad op rejects
the whole call and nothing changes. `dry_run=true` returns the plan. `help(topic='ops')` lists every
op, `help(op=NAME)` shows its fields and a valid example (also: `inkscape://ops`).

Reversibility. Every real edit takes ONE snapshot and writes ONE Operation Record (ADR-004); a
genuine no-op writes nothing and reports `changed: false`. To undo: `history(doc_id,
action='undo')` (again = one edit further back) or the `undo` call from the edit result, verbatim;
never pick a snapshot by guessing (an edit's `snapshot_id` is the state before THAT edit). Undo is
itself reversible; 'restore' + snapshot_id, 'create' checkpoints, 'list' browses, 'reload'
re-reads the source file.

Risk classes & approval. low (inspect/render/help/export), medium (edit ops: reversible,
snapshot-backed), high (delete, set_svg/insert_svg, path ops, overwriting a file, running Actions:
needs an explicit per-call `approval_token`), restricted (never ships). A call's risk
is the MAX over its ops; a high-risk call without a non-empty `approval_token` is refused.
A token approves ONE call: ask the human for it (show the dry_run plan) before each high-risk
call, never invent one and never reuse a token from an earlier, different change.

Working with the human. Send ids exactly as the human wrote them, even if you suspect a typo
(`brand-nam`): never pre-correct. Only after the server's `not found (similar: ...)` error retry
with the match, and tell the human which id you used.
Reply in the human's language; `help(goal=...)` understands non-English goals, so pass their
words as they are.

Look before you trust. `edit(..., render='inline')` and `render(doc_id)` return a PNG you can see;
check it, and `history(action='undo')` if it is wrong. `inspect(doc_id, include=[...],
select=...)` reads structure, objects, validation and quality.

Finding the right call. Don't grep the surface: `help(goal='make the svg smaller')` returns the
`tool:op` to use plus an example call (and flags out-of-scope goals); `help(topic='capabilities')`
shows what this runtime supports and `help(topic='diagnose')` re-probes it. `live` / `live_edit`
steer a running Inkscape when live mode is enabled. The generated `llms.txt` / `llms-full.txt` and
`docs/agent-usage-guide.md` carry the full detail.
"""

__all__ = ["SYSTEM_OVERVIEW"]
