"""In-context system overview delivered as MCP server ``instructions`` (E19-02).

The Penpot survey's real onboarding edge is that its ``high_level_overview`` is delivered as MCP
server **instructions** — so the model holds the document model + idioms every turn, not only if it
happens to open a ``docs/`` file. Our equivalent prose already exists in
``docs/agent-usage-guide.md`` (E15-05), but that sits OUTSIDE the agent's context. This module
delivers a CONCISE orientation in-context via the FastMCP ``instructions`` field.

It is deliberately a SHORT orientation + pointers, NOT a second copy of the guide: it states the
load-bearing invariants (document model + ``doc_id`` lifecycle, the working-copy + snapshot/restore
reversibility idiom, the risk classes + ``approval_token`` gate, the intended tool ordering, and the
render-and-look default — E19-03) and then routes to the authoritative, generated discovery surface
(``how_do_i`` / ``list_capabilities`` / ``llms.txt``) and the full guide for everything else. The
deep detail stays single-sourced in the guide + the generated manifest; this never restates it.

Audit (E19-02): before this module the server was constructed as ``FastMCP("inkscape-mcp")`` with NO
``instructions`` field — E17/E18 wired tool ANNOTATIONS, TAGS, progressive disclosure, and the
``inkscape://prompts`` index, but no always-in-context overview. This closes that gap.
"""

from __future__ import annotations

#: The concise, always-in-context overview handed to FastMCP as ``instructions``. Kept short on
#: purpose — it orients, then points at the generated discovery surface for specifics.
SYSTEM_OVERVIEW = """\
inkscape-mcp makes Inkscape/SVG documents agent-ready through SMALL TYPED TOOLS (not a free-text
run_action / execute_code portmanteau). Orientation:

Document model & lifecycle. Work is keyed by a `doc_id`: `create_document` (new blank) or
`open_document` (existing workspace SVG) returns one; every other tool takes it. Typical flow:
open/create -> inspect (`inspect_document` / `find_objects` for object ids) -> edit (typed DOM
tools) -> `render_preview` to look -> `export_document` / `save_document_as`. All writes land on a
WORKING COPY; the original source file is never modified.

Reversibility. Every real mutation auto-snapshots first and emits an Operation Record (ADR-004). A
genuine no-op writes nothing and reports `changed: false`. Undo with
`restore_snapshot(doc_id, snapshot_id)`; `create_snapshot` checkpoints on demand; `list_snapshots`
browses; `reload_document` re-reads the working copy from disk.

Risk classes & approval. Each tool declares a risk class: low (read/inspect/render/export),
medium (create/style/text/transform — reversible, snapshot-backed), high (overwrite/delete/path
geometry/Action chains — requires a per-operation `approval_token`, minted out of band), restricted
(never ships). A high-risk tool refuses without a non-empty `approval_token`.

Batching. `apply_edits` applies an ordered list of typed edits as ONE atomic, reversible operation
(validate-all first, all-or-nothing, one snapshot) — use it to make several edits in a single call
instead of N round-trips. Its effective risk is the max over its members.

Render and look before you trust an edit. After a mutating call (especially a batch), render and
INSPECT the result — `render_preview` (headless) or `live_render_view` (live mode) — before relying
on it; `restore_snapshot` reverts if it is wrong.

Finding the right tool. Don't grep the surface: call `how_do_i(goal)` (natural-language goal ->
tool names + how-to, and it flags out-of-scope goals), or `list_capabilities` for the runtime matrix
plus the full intent map. The generated `llms.txt` / `llms-full.txt` manifest and
`docs/agent-usage-guide.md` carry the full per-tool detail.
"""

__all__ = ["SYSTEM_OVERVIEW"]
