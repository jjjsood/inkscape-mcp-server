# Agent usage guide — driving inkscape-mcp from an agent

How to drive this server from an LLM agent: the core create→render→export loop, the
working-copy + snapshot reversibility model, the risk classes and the approval-token gate for
HIGH-risk tools, and how to pick the right tool. The surface is **98 small typed tools / 7 prompts /
16 resources** — deliberately *not* a portmanteau `run_action(string)` / `do_task(prompt)` design
(ADR-002/003). The trade-off: more tools to navigate, but each is explicit, typed, and risk-classed.
Use the discovery tools below instead of grepping the list.

The server also ships a concise **system overview as MCP `instructions`** (E19-02), delivered
in-context every turn (the document model + `doc_id` lifecycle, the snapshot/restore reversibility
idiom, the risk classes + the `approval_token` gate, the intended tool ordering, and the
render-and-look default below). This page is the fuller companion to that always-in-context summary.

This is the agent-facing companion to the two machine-readable manifests
[`llms.txt`](../llms.txt) (concise index) and
[`llms-full.txt`](../llms-full.txt) (full per-tool manifest),
both **generated from the live registry** by `scripts/gen_llms_txt.py`.

---

## 1. Picking the right tool

Three read-only discovery tools answer "which tool do I call?" without reading source:

- **`how_do_i(goal)`** — map a natural-language goal ("draw a rectangle", "make my svg smaller for
  web", "find the red shapes") to the concrete tool name(s) + a one-line how-to. It also flags
  out-of-scope goals (raster/pixel editing, arbitrary Actions/extensions/scripts, network fetch, code
  execution) with the reason, instead of mis-routing them. Guidance only — it executes nothing.
- **`list_capabilities`** — the runtime capability matrix (Inkscape version, available Actions,
  export formats, live availability, fonts) **plus** the full `intents` goal→tool map (same data
  `how_do_i` matches against). Browse the whole map at once here.
- **`find_objects(doc_id, …)`** — resolve the **object ids** that the id-taking edit tools need
  (filter by fill/stroke/tag/text/id_prefix/bbox). `inspect_document` gives the same ids plus the
  full structure (tree, layers, styles, fonts, assets).

The README's [“Try asking your agent to…”](../README.md#try-asking-your-agent-to)
catalog lists representative asks per group; it is aligned with the same `intents.py` map so the doc
and the tools never diverge.

Rule of thumb: simple structural edits (create/style/text/transform) go through the direct `lxml`
DOM layer; render, export, and complex path geometry go through the Inkscape engine (ADR-005).

---

## 2. The create → render → export loop

The generative flow (E14). All writes land on a **working copy**, never the original file.

1. **Create or open a document.**
   - `create_document(width, height, units)` — a brand-new blank tracked document (no source file
     required). Returns a `doc_id` used by every other tool.
   - `open_document(path)` — open an existing workspace SVG as a working copy; also returns a `doc_id`.

2. **Draw / compose.** Add elements with the typed creation tools:
   `create_rect`, `create_circle`, `create_ellipse`, `create_line`, `create_polygon`,
   `create_polyline`, `create_path`, `create_text`; group with `create_group` / `group_objects`;
   reuse with `create_use`; add gradients with `add_linear_gradient` / `add_radial_gradient`.
   Each returns the new `object_id` (+ analytic bbox), so the next call can target it.

3. **Style.** `set_fill`, `set_stroke`, `set_opacity`, `set_font`, `replace_text` take an `object_id`
   (use `find_objects` to resolve one). `replace_color` / `apply_palette` recolor document-wide.

   **Batch several edits in one call.** `apply_edits(doc_id, edits)` applies an ordered list (≤ 64) of
   **typed** edits — a discriminated union over the DOM ops (each tagged by an `op` field, e.g.
   `{"op": "create_rect", …}`, `{"op": "set_fill", …}`, `{"op": "move_object", …}`) — through the
   SAME edit kernel as ONE atomic, reversible operation: validate-all first (one bad edit leaves the
   document byte-identical), all-or-nothing rollback, and a single snapshot + Operation Record (one
   `restore_snapshot` reverts the whole batch). Effective risk = MAX over members (a `delete_object`
   member escalates the batch to HIGH and needs an `approval_token`). Prefer it over N round-trips
   when you already know the edits; path geometry and cross-document composition are not batchable.

4. **Render and look before you trust the edit (a loud default).** `render_preview(doc_id, …)`
   rasterizes the working copy and returns the PNG **inline** as an MCP image content block (when
   under the inline byte threshold) — so the agent can *look* at what it just made and decide the
   next edit, without writing a file. Set `inline=False` (or exceed the threshold) to get the
   artifact path instead. Make this the default close of every mutating step (especially an
   `apply_edits` batch, which changes several things at once): render, inspect, and `restore_snapshot`
   if it is wrong. In live mode the equivalent is `live_render_view`.

5. **Export the final artifact.** `export_document` writes a PNG/PDF/SVG/etc.; `export_object`
   exports a single object; `export_batch` / `create_icon_set` produce many sizes/formats at once;
   `export_web_profile` / `export_print_profile` apply a web/print preset; `svg_web_optimize`
   losslessly shrinks the SVG first. Save the SVG itself with `save_document_as`.

Every artifact-producing tool returns a root-relative `workspace_relative_path` (and a managed
`artifact_path`) — **never an absolute host path** (sec.12).

---

## 3. Working copy, snapshots, and reversibility

The server is safe to hand to an autonomous agent because every mutation is reversible and originals
are never touched:

- **Working copies.** `open_document` opens a copy; no tool overwrites the source. Persisting writes
  elsewhere via `save_document_as` (overwriting an existing file is itself HIGH-risk — see below).
- **Operation Records + snapshots (ADR-004).** Every *real* mutating op snapshots the working copy
  first and emits an **Operation Record** (what changed, the risk class, the policy decision,
  before/after preview). A genuine no-op (e.g. `set_fill` to the colour already present,
  `replace_color` matching nothing) reports `changed: false` and writes **no** snapshot or record —
  nothing happened, so nothing clutters the history.
- **Undo / restore.** `create_snapshot` checkpoints on demand; `list_snapshots` browses the history;
  `restore_snapshot(doc_id, snapshot_id)` rolls the working copy back to any indexed snapshot.
  `reload_document(doc_id)` re-reads the working copy from disk (e.g. after an external/live edit).
- **Retention.** Snapshot/artifact/live-frame growth is bounded by an **explicit** sweep (boot-time +
  the `prune_snapshots` tool) — never an implicit side effect of a mutating tool.

Practical loop: snapshot (or rely on the automatic pre-mutation snapshot) → edit → `render_preview`
to inspect → if wrong, `restore_snapshot` back and try again.

---

## 4. Risk classes and the approval-token gate

Every tool declares a risk class (in its docstring and in `llms.txt`):

| Class | What | Gate |
|---|---|---|
| **low** | read / inspect / validate / quality / render / export / discovery | always permitted |
| **medium** | write-new / element-creation / style / text / transform / web-optimize | permitted; reversible, snapshot-backed |
| **high** | overwrite an existing file · path geometry (`simplify_path`, `cleanup_paths`, `combine_paths`, `boolean_union`/`boolean_difference`, `stroke_to_path`, `break_apart`) · Action chains (`run_action_chain`) · raw Action (`run_raw_action`) | requires a per-operation **`approval_token`** |
| **restricted** | code / network / fs-escape | never ships in the MVP |

**The approval gate.** A HIGH-risk tool refuses (`high-risk operation requires explicit approval`)
unless it is called with a non-empty `approval_token`. The token is **minted/confirmed out of band
and bound to a single operation** — it is deliberately *not* an ambient env flag or a setting the
model can flip on for itself. Many HIGH-risk path tools also default to `dry_run=True`: call them
once to validate + preview the change with no mutation, then call again with `dry_run=False` **and**
the `approval_token` to apply it. Compose/adopt tools (`tools/compose.py`) are HIGH + approval-gated
for the same reason (they ingest arbitrary SVG).

So a typical HIGH-risk flow is: `how_do_i` → dry-run the tool to preview → obtain an `approval_token`
out of band → re-call with the token to apply → `render_preview` to confirm → `restore_snapshot` if
unhappy.

---

## 5. Resources and prompts

- **Resources** (read-only, addressable by URI): `inkscape://runtime/capabilities`,
  `inkscape://documents` (index of open docs), `inkscape://document/{doc_id}/{summary,tree,layers,
  objects,styles,fonts,assets}`, and the `inkscape://live/*` set. Read these for state instead of
  re-deriving it.
- **Prompts** (orientation only, grant no capability): `live_canvas_assist`, `prepare_web_export`,
  `prepare_icon_set`, `prepare_print_export`, `theme_recoloring`, and the E15-04 authoring pair
  `compose_artwork` / `restyle_artwork`. They tell the agent *how* to drive the relevant tools safely.

---

## 6. Out of scope

`how_do_i` returns an explicit reason for these rather than a tool: raster/photo **pixel** editing
(this is a vector/SVG server), arbitrary Inkscape Actions / extensions / scripts (ADR-003 — no
free-text escape hatch in the MVP surface), network/URL fetch (offline by policy, sec.12), and
arbitrary code execution (restricted). Provide local workspace files; use the typed tools.
