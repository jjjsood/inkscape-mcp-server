# Agent usage guide — driving inkscape-mcp from an agent

How to drive this server from an LLM agent: the `open` → `edit` → `export` loop, ops with `$n`
references and selectors, the working-copy + snapshot reversibility model, the risk classes and the
approval-token gate for HIGH-risk calls, and how to find the right call. The surface is **ten
result-oriented tools / 7 prompts / 17 resources** — deliberately *not* a portmanteau
`run_action(string)` / `do_task(prompt)` design (ADR-002/003), and deliberately *not* one tool per
operation either: the typed operations are **ops** inside a few workflow tools, so a drawing takes
two or three calls, not twenty.

The server also ships a concise **system overview as MCP `instructions`**, delivered in-context
every turn (the `doc_id` lifecycle, the flow below, the snapshot/restore idiom, the risk classes +
the `approval_token` gate, and the render-and-look default). This page is the fuller companion to
that always-in-context summary.

This is the agent-facing companion to the two machine-readable manifests
[`llms.txt`](../llms.txt) (concise index) and [`llms-full.txt`](../llms-full.txt) (full per-tool and
per-op manifest), both **generated from the live registry** by `scripts/gen_llms_txt.py`.

---

## 1. The tools

| Tool | Use it to |
|---|---|
| `open` | Start working: a workspace file (`path`), SVG text (`svg`), a blank page (`canvas`) or the live canvas (`from_live`). Returns a `doc_id`; optional `ops` make the first edit in the same call. |
| `inspect` | Read a document (or several): `include` ⊂ summary, objects, tree, layers, styles, fonts, assets, validate, quality, snapshots, frames; `select` filters `objects`. |
| `edit` | Change a document with **typed ops** in ONE atomic call (≤ 64 ops, one snapshot, one Operation Record). |
| `render` | Look at the page, one object or a numbered frame as an inline PNG. |
| `export` | Write files (png/pdf/svg), profiles (web/icons/print) or `save_as`, for one document or a set. |
| `history` | `list` / `create` / `restore` snapshots, `prune`, `reload` from the source file. |
| `help` | `goal=…` → the right `tool:op` + an example; `op=…` → an op's fields, risk and a valid example; `topic=ops\|capabilities\|diagnose`; `stat_paths=[…]`. |
| `live`, `live_edit` | Observe and steer a **running** Inkscape (only when live mode is enabled). |
| `actions` | *Advanced mode only.* Allow-listed Inkscape Actions (see §6). |

Rule of thumb: structural edits go through the direct `lxml` DOM layer; render, export and complex
path geometry go through the Inkscape engine (ADR-005). You never pick the layer — the op decides.

---

## 2. The open → edit → export loop

All writes land on a **working copy**, never the original file.

1. **Open.** One call returns the `doc_id` every other tool takes — and can already draw:

   ```json
   open(canvas={"width": 800, "height": 600, "background": "#ffffff"},
        ops=[{"op": "create_circle", "cx": 400, "cy": 300, "r": 100, "fill": "#ff6633", "id": "sun"}],
        render="inline")
   ```

   (`open(path="logo.svg")` for an existing workspace file; `open(svg="<svg …>")` for SVG text.)

2. **Edit.** `edit(doc_id, ops=[…], render="inline")` applies many ops at once and returns the ids it
   created, the new snapshot id, the call's risk and a picture of the result:

   ```json
   {"doc_id": "d1", "changed": true, "snapshot_id": "snap-0002", "risk": "medium",
    "created": [{"op": 0, "id": "bg", "bbox": [0, 0, 800, 600]}, {"op": 1, "id": "title", "bbox": [40, 80, 320, 48]}]}
   ```

   - **`$<n>` references.** `"$0"` names the id that op 0 of *this call* created, so an object can
     be styled, grouped or moved in the op right after it is made — the whole drawing is one call.
   - **Selectors instead of lookups.** An op that acts on objects takes `ids=[…]` **or**
     `select={tag, fill, stroke, text, id_prefix, bbox}`. The selector is resolved when the op runs,
     against the tree including objects created earlier in the same call.
   - **Atomic.** Every op is validated before anything changes; one bad op rejects the whole call
     and the document stays byte-identical. Errors say what to do:
     `ops[2] fill: id 'logo2' not found (similar: logo, logo-bg)`.
   - **`dry_run=true`** returns the plan (matched ids, risk, whether approval is needed) without
     changing anything. **`detail`** = `ids` (default) | `summary` (one line per op) | `full`.
   - The op families: **create** (shapes, text, groups, gradients, `use`), **style** (`fill`,
     `stroke`, `opacity`, `replace_color`, `palette`, `font`), **object** (`replace_text`,
     `duplicate`, `rename`, `delete`), **transform** (`move`, `scale`, `rotate`, `tile`,
     `resize_canvas`, `normalize_viewbox`, `fit_to_content`), **compose** (`set_svg`, `insert_svg`,
     `place`, `grid`), **path** (HIGH, advanced mode) and **optimize** (`optimize_web`).
     `help(topic="ops")` lists them; `help(op="fill")` shows the fields and a valid example; the
     `inkscape://ops` resource has the full reference.

3. **Look before you trust the edit (a loud default).** `render="inline"` returns the PNG as an MCP
   image block, so the agent *sees* what it made and decides the next edit — no file, no extra
   call. `render(doc_id, target="object", object_id="logo")` zooms to one object. Close every
   mutating step with a look; if it is wrong, `history(action="restore")`. In live mode the
   equivalent is `live_edit(observe="render")`.

4. **Export.** One call can write several artifacts:

   ```json
   export(doc_id, targets=[{"kind": "file", "format": "png", "width_px": 1024},
                           {"kind": "file", "format": "svg"},
                           {"kind": "profile", "name": "icons"}])
   ```

   `profile` targets are `web` (responsive PNGs + SVG), `icons` (square PNG ladder) and `print`
   (print-ready PDF); `save_as {dest_path, overwrite}` writes the working copy to a workspace file.
   `dry_run=true` projects sizes without writing and `byte_budget` caps the total. A list of
   `doc_id`s exports a set (files are prefixed with each id). To shrink first:
   `edit(doc_id, ops=[{"op": "optimize_web"}])`.

Every artifact comes back as a workspace-relative path — **never an absolute host path** (sec.12).

---

## 3. Finding the right call

- **`help(goal="make the svg smaller")`** maps a natural-language goal to the `tool:op` to use, a
  one-line how-to and an example call (`edit:optimize_web`, `export:profile`, …). It also flags
  out-of-scope goals with the reason instead of mis-routing them. Guidance only — it executes
  nothing.
- **`help(op=NAME)`** and **`help(topic="ops")`** document the ops; **`help(topic="capabilities")`**
  is the runtime matrix (Inkscape version, export formats, live availability, fonts);
  **`help(topic="diagnose")`** re-probes the runtime when something does not work.
- **`inspect(doc_id, select={"fill": "#ff0000"})`** resolves ids (rows of `[id, tag, bbox, fill]`);
  `include=["tree", "styles", …]` gives the structure. You rarely need it before an edit, because
  edit ops take `select` themselves.

The README's [“Try asking your agent to…”](../README.md#try-asking-your-agent-to) catalog lists
representative asks per group; it is aligned with the same `intents.py` map, and a test checks that
every `(tool, op)` it names exists, so the doc and the tools cannot diverge.

---

## 4. Working copy, snapshots, and reversibility

The server is safe to hand to an autonomous agent because every mutation is reversible and
originals are never touched:

- **Working copies.** `open` opens a copy; no tool overwrites the source. Persisting writes
  elsewhere via `export` with a `save_as` target (overwriting an existing file is itself HIGH risk).
- **Operation Records + snapshots (ADR-004).** Every *real* `edit` takes ONE snapshot of the working
  copy and writes ONE **Operation Record** (what changed, the risk class, the policy decision). A
  genuine no-op (e.g. `fill` to the colour already present, `replace_color` matching nothing)
  reports `changed: false` and writes **no** snapshot or record.
- **No hidden renders.** An edit renders nothing unless you ask (`render="inline"`); set
  `INKSCAPE_MCP_EDIT_PREVIEWS=after|before_after` to keep before/after PNGs in the Operation
  Record for audit.
- **Undo / restore.** `history(doc_id, action="list")` browses; `"create"` checkpoints on demand;
  `"restore"` + `snapshot_id` rolls the working copy back (itself reversible: it snapshots first);
  `"reload"` re-reads the working copy from the source file, e.g. after an external edit.
- **Retention.** Snapshot, artifact and live-frame growth is bounded by an **explicit** sweep
  (boot-time + `history(action="prune")`) — never an implicit side effect of an edit.

Practical loop: edit (an automatic snapshot is taken) → look → if wrong, `restore` and try again.

---

## 5. Risk classes and the approval-token gate

Every call has a risk class (per tool in `llms.txt`; per op in `help(op=…)`):

| Class | What | Gate |
|---|---|---|
| **low** | `inspect`, `render`, `help`, `export` of files | always permitted |
| **medium** | `edit` / `open` ops that create, style, move, transform or optimize | permitted; reversible, snapshot-backed |
| **high** | `delete`, `set_svg`, `insert_svg`, the path ops (`simplify`, `union`, `difference`, `combine`, `break_apart`, `stroke_to_path`, `cleanup`), `export` with an overwriting `save_as`, `live_edit`, running `actions` | requires a per-operation **`approval_token`** |
| **restricted** | code / network / fs-escape | never ships |

The risk of an `edit` call is the **maximum over its ops** — one `delete` among twenty `create_*`
ops makes the call HIGH. The error tells you what to do: `ops[3] delete is HIGH risk; re-call with
approval_token`.

**The approval gate.** A HIGH-risk call is refused unless it carries a non-empty `approval_token`.
The token is **minted/confirmed out of band and bound to a single operation** — deliberately *not*
an ambient env flag or a setting the model can flip on for itself. Use `dry_run=true` first to see
the plan with no mutation, then re-call with the token to apply.

So a typical HIGH-risk flow is: `help(op=…)` → `edit(…, dry_run=true)` to preview → obtain an
`approval_token` out of band → re-call with the token → look (`render`) → `history(action="restore")`
if unhappy.

---

## 6. Live mode and advanced actions

- **`live`** observes a running Inkscape: `live(action="observe", include=["selection", "inspect",
  "view"])` returns the selection, its object details and an inline canvas PNG in one call, and
  connects automatically. `wait` blocks until the canvas changes, `viewport` moves the view, `diff`
  shows what a `live_edit` changed. **`live_edit`** (`apply` a `style`/`transform`, `insert_svg`,
  `set_text`) is always HIGH risk. Both exist only when `INKSCAPE_MCP_LIVE_ENABLED` is on (the
  default); `live(action="check")` shows what this host supports. To keep the live canvas as a
  tracked document: `open(from_live={"dest_path": "…"})`.
- **`actions`** (advanced mode, `INKSCAPE_MCP_RAW_ACTION_ENABLED=1`): `list` what the runtime
  exposes, `validate` a chain of `{action, args}` steps, `run` it (HIGH, `approval_token`), `raw`
  for one step. Prefer an edit op whenever one exists; the path ops need the same flag.

---

## 7. Resources and prompts

- **Resources** (read-only, addressable by URI): `inkscape://ops` (the op reference),
  `inkscape://runtime/capabilities`, `inkscape://runtime/intents`, `inkscape://prompts`,
  `inkscape://documents` (index of open docs), `inkscape://document/{doc_id}/{summary,tree,layers,
  objects,styles,fonts,assets}` and the `inkscape://live/*` set. Read these for state instead of
  re-deriving it.
- **Prompts** (orientation only, grant no capability): `compose_artwork` / `restyle_artwork`,
  `prepare_web_export`, `prepare_icon_set`, `prepare_print_export`, `theme_recoloring` and
  `live_canvas_assist`. They tell the agent *how* to drive the relevant tools safely.

---

## 8. Out of scope

`help(goal=…)` returns an explicit reason for these rather than a call: raster/photo **pixel**
editing (this is a vector/SVG server), arbitrary extensions / scripts / macros (ADR-003 — only the
allow-listed `actions` tool, and only when the operator enables it), network/URL fetch (offline by
policy, sec.12), and arbitrary code execution (restricted). Provide local workspace files; use the
typed ops.
