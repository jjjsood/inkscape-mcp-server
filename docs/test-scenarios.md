# Workflow test scenarios

These scenarios test the server the way an agent actually uses it: **one long session per task**,
where every step builds on the results of the previous ones. You look, change, look again, take
feedback, make mistakes, roll back, ship files and change them once more. What they check is the
**interplay** of the tools: ids that survive many edits, history that stays exact over many rounds,
undo after delivery, multi-document work, risk gates in the middle of real work.

Single functions (one error text, one `help` lookup, one blank-token check) are **not** tested
here. The unit tests cover them (`tests/test_actionable_errors.py`, `tests/test_help_ops.py`,
`tests/test_risk_for.py`, `tests/test_inspect_*.py`, …). Where an agent would hit such a behaviour
naturally, it shows up as a step inside a workflow.

Out of scope: live mode (`live` / `live_edit`).

There are two paths. Both use the same IDs:

| Path | What | Checks |
|---|---|---|
| **A. Script** | `scripts/scenario_smoke.py` starts the server over STDIO and plays WF1–WF8 | that the server computes the right results across a whole workflow |
| **B. Agent** | you give an agent (Claude Desktop / Claude Code) the prompt chain below, turn by turn, and grade it with the checklist | that an agent uses the surface well: tool choice, call count, behaviour at HIGH risk, recovery |

`tests/e2e/test_workflows.py` is the CI counterpart (pytest, `@pytest.mark.inkscape`). It overlaps
with WF2–WF5 on purpose and is not driven by this document.

---

## Prerequisites

1. Inkscape 1.x on `PATH` (`inkscape --version`). Without it, path A runs the DOM steps of every
   workflow, skips the engine steps and reports the workflow as `SKIP`.
2. Dependencies installed: `uv sync` in `inkscape-mcp-server/`.
3. An empty folder as workspace, e.g. `~/mcp-test`, with one subfolder per workflow.
4. For path B: a host config following [`examples/mcp.json`](../examples/mcp.json) with
   `INKSCAPE_MCP_WORKSPACE_ROOTS=<test folder>` and `INKSCAPE_MCP_LIVE_ENABLED=0`.
   **WF6 additionally needs `INKSCAPE_MCP_RAW_ACTION_ENABLED=1`.** Path ops are operator-gated,
   and without the flag every path op fails with
   `path ops are disabled; the operator must set INKSCAPE_MCP_RAW_ACTION_ENABLED=1`.
   Path A always sets the flag.
5. Fixtures for path B, copied into the workflow's folder before the run:
   - `logo.svg`:

     ```xml
     <?xml version="1.0" encoding="UTF-8"?>
     <svg xmlns="http://www.w3.org/2000/svg" width="200" height="120" viewBox="0 0 200 120">
       <rect id="bg" x="0" y="0" width="200" height="120" fill="#ff0000"/>
       <circle id="dot" cx="100" cy="60" r="30" fill="#ff0000"/>
       <text id="label" x="20" y="100" fill="#222222">Hello</text>
     </svg>
     ```

   - `poster.svg` = [`tests/e2e/fixtures/brand_poster.svg`](../tests/e2e/fixtures/brand_poster.svg):
     a real Inkscape file, 400×300, three layers (`layer-bg` locked, `layer-content`,
     `layer-footer`), CSS class `.accent` (`#e63946`), gradient, `<symbol>`, tspans.
   - `grid.svg`: 120 rects `c{r}_{c}` on a 40 px grid (12 columns, 10 rows), `#cc0000` when
     `(r+c) % 3 == 0`, otherwise `#0055cc`. Generate it with `grid_svg()` from
     `scripts/scenario_smoke.py`.

   Record `sha256sum` of every source file first, so you can check that originals stay untouched.

---

## Path A: script

```bash
cd inkscape-mcp-server
uv run python scripts/scenario_smoke.py --list              # list workflows, call nothing
uv run python scripts/scenario_smoke.py                     # all, server over STDIO (uv run inkscape-mcp)
uv run python scripts/scenario_smoke.py --only WF1,WF6      # a subset
uv run python scripts/scenario_smoke.py --require-inkscape  # SKIP counts as FAIL
uv run python scripts/scenario_smoke.py -v --keep           # call trace on FAIL, keep the workspace
uv run python scripts/scenario_smoke.py --in-process        # no subprocess (in-memory client)
uv run python scripts/scenario_smoke.py --cmd "inkscape-mcp"  # test the installed version
```

The script creates a temporary workspace with one subfolder per workflow and prints
`PASS` / `FAIL` / `SKIP` with the call count per workflow. Exit code 1 as soon as one workflow
FAILs. `--json` prints the report (including checks passed and seconds) as JSON.

---

## Path B: agent workflows

Each workflow is a **prompt chain**: give turn 1 verbatim, wait until the agent is done, then give
the next turn. Grade with the checklist at the end. "Calls" counts tool calls to this server only.
The budget is a guide value for a good run, not a hard limit. A workflow `PASS`es when every
checklist item holds; `PARTIAL` when the result is right but the path was clumsy (way over budget,
needless lookups, retries).

### WF1 Logo from scratch → feedback rounds → delivery → late fix

Fixture: none (blank canvas). Budget: ≈ 10 calls.

1. > Draw a logo on a new 200×120 canvas: blue background (#3366cc) with id `bg`, a white circle
   > `mark` (radius 25) in the upper middle and below it the white text "Logo", centred. Group
   > circle and text as `brand`. Show me the result.
2. > Make the circle 30% bigger, but keep it where it is.
3. > Actually, a teal background (#2a9d8f) looks better.
4. > The name is "Acme", not "Logo". Show me the circle on its own, large.
5. > Good. Deliver it: the SVG as `deliver/logo.svg`, plus the icon set and the web profile.
6. > The client wants the circle orange (#f4a261). Update the delivered file.

- [ ] Turn 1 is **one** `open(canvas, ops, render="inline")`; `group` uses `"$1"`/`"$2"`, no
      lookup call in between
- [ ] Turn 2: `scale` about its own centre (centre stays at 100/50, width 65)
- [ ] Turns 2–4: one `edit` per round, each with a look at the result (`render="inline"` or `render`)
- [ ] Turn 4: `render(target="object", object_id="mark")`
- [ ] Turn 5: `save_as` without a token; icon + web artifacts with workspace-relative paths
- [ ] Turn 6: the first overwrite attempt fails with either `… overwriting requires overwrite=True
      and an approval_token` or, when `overwrite=True` was sent without a token, `destination
      already exists; overwrite=True also needs an approval_token (overwriting is HIGH risk)`; the agent **asks for a token** instead of inventing one; with the token,
      `saved.overwritten: true` and the file contains `#f4a261`
- [ ] `history(action="list")` shows exactly one snapshot per edit round

### WF2 Brand refresh of a layered poster

Fixture: `poster.svg`. Budget: ≈ 10 calls.

1. > Open `poster.svg` and tell me what's in it: layers, colours, anything locked.
2. > Our accent #e63946 becomes #2a9d8f everywhere, including where it comes from CSS classes.
   > The headline "Spring Sale" becomes "Summer Sale". Make the CTA button `cta` 20% bigger but
   > keep it in place. Add a small white text "NEW" at the bottom right of the content area. Do it
   > in one go and show me the result.
3. > Make it web-ready but keep it editable in Inkscape (layers stay). Save it as `poster-v2.svg`
   > and export the web profile.
4. > Now a ship-only copy without any editor data, as `poster-ship.svg`.

- [ ] Turn 1: `inspect(include=[layers, styles])` reports `layer-bg` as `locked: true`
- [ ] Turn 2 is **one** `edit` (palette + replace_text + scale + create_text) with a look at the
      result; no `#e63946` left, also not in the CSS (`inspect(include=['styles'])`)
- [ ] `cta` centre unchanged, width ×1.2; "NEW" lands in `layer-content`, not in the locked layer
- [ ] Turn 3: default `optimize_web` keeps the three layers and the lock; `poster-v2.svg` has them
- [ ] Turn 4: `optimize_web(aggressive=true)` is HIGH → the agent asks for a token;
      `poster-ship.svg` contains no `sodipodi:namedview`
- [ ] `sha256sum poster.svg` unchanged

### WF3 Iteration with mistakes and recovery

Fixture: `poster.svg`. Budget: ≈ 20 calls.

1. > Open `poster.svg` and set a checkpoint "good".
2. > Move the header 900 px to the right. … Oops, that was wrong, go back to "good".
3. > In ONE edit: add a white rectangle `badge` (80×36 at 300/180), change the headline to
   > "Final Sale", and colour the brand name `brand-nam` #1d3557.
4. > Colour the brand name #1d3557 again.
5. > Delete the star `burst`.
6. > On second thought, bring the star back.
7. *(Outside the agent: replace "Spring Sale" with "Autumn Sale" in `poster.svg`.)*
   > I changed the file outside. Load it again.
8. > That threw away my edits. Get them back and tidy up the history.

- [ ] Turn 2: `restore` to the checkpoint, document exactly as before
- [ ] Turn 3: first call fails with `ops[2] fill: id 'brand-nam' not found (similar: brand-name, …)`;
      nothing applied, no snapshot; the agent fixes the id and repeats the **whole** call once
- [ ] Turn 4: `changed: false`, no snapshot; the agent reports "already that colour"
- [ ] Turn 5: refused without a token; the agent shows the plan (`dry_run`) and asks for a token;
      the result's `undo` names a `history(…restore…)` call
- [ ] Turn 6: restore with the delete's `snapshot_id`: `burst` is back, `badge` and "Final Sale" stay
- [ ] Turn 7: `history(action="reload")`; the headline is now "Autumn Sale", `badge` is gone
- [ ] Turn 8: restore with `pre_reload_snapshot_id` brings `badge` back; `prune` reports `retained`

### WF4 Variants and contact sheet

Fixture: `poster.svg` three times (`poster-a.svg`, `poster-b.svg`, `poster-c.svg`). Budget: ≈ 15 calls.

1. > Three variants: A = original, B = headline "Sale B" and #e63946 → #2a9d8f, C = headline
   > "Sale C" and #e63946 → #e9c46a. Compare the quality reports of all three at once.
2. > Put all three side by side on one sheet (1240×300) and show it to me.
3. > Change B's headline to "Sale B2". Is the sheet up to date?
4. > Export all three variants as PNG (400 px) in one go, and the sheet as PDF and as
   > `sheets/contact.svg`.

- [ ] Three `doc_id`s; one multi-document `inspect` with `docs` per variant plus `consistency`
- [ ] B and C have no `#e63946` left (CSS included), A unchanged
- [ ] Sheet via `grid` (`doc_ids`) in one `open(canvas, ops)` call; **each variant keeps its own
      accent colour on the sheet** (A red, B teal, C yellow)
- [ ] Turn 3: the agent notices the sheet holds a copy (no "Sale B2" on it) and rebuilds it
- [ ] Set export: one PNG per variant, file names contain the `doc_id` (`<stamp>-<doc_id>-…`)
- [ ] `save_as` with a list of `doc_id`s would fail with `call export once per doc_id`; the agent
      saves the sheet on its own

### WF5 Bulk edits by selector

Fixture: `grid.svg`. Budget: ≈ 10 calls.

1. > In `grid.svg`: all red rects in the top-left quarter (x < 240, y < 200) become yellow (#ffcc00),
   > all other red ones teal (#2a9d8f), and all blue ones move 2 px to the right. How many did each
   > action hit?
2. > Also add ten small green squares in a row at the top and delete `c0_1`, in the same edit.
3. > Forget the delete, just the squares.
4. > Delete all teal rects. How many are those?
5. > How big would a 4000 px PNG be? My budget is 1 KB.

- [ ] Turn 1: one `edit` with three selector ops; counts 10 / 30 / 80; one snapshot. A default
      `max_matches` (64) below 80 fails clearly, and the agent raises `max_matches` instead of
      splitting the call
- [ ] Turn 2: refused with `ops[10] delete is HIGH risk; …`; **none** of the ten squares exists
- [ ] Turn 3: the ten creates run at `risk: "medium"` without a token
- [ ] Turn 4: `dry_run` shows 30 matches first; deleted only with a token
- [ ] Turn 5: `export(dry_run=true, byte_budget=1000)` → `within_budget: false`, no files written

### WF6 Path ops: icon from primitives

Fixture: none. Needs `INKSCAPE_MCP_RAW_ACTION_ENABLED=1`. Budget: ≈ 12 calls.

1. > On a 64×64 canvas: a navy (#264653) square `plate` (48×48 at 8/8), a navy circle `knob`
   > (r 8) centred on the top edge, a black circle `hole` (r 10) in the middle and an orange
   > (#f4a261) ring `ring` (r 28, stroke 3, no fill).
2. > Merge `knob` into `plate` as one shape.
3. > Punch `hole` out of the plate and show me the result.
4. > Turn the ring's stroke into a real shape and simplify it.
5. > Combine plate and ring into one path. … Now break it apart again. … No, keep it combined.
6. > Export a 128 px PNG and the SVG as `icon-paths.svg`.

- [ ] Every path op is HIGH: the agent shows the plan (`dry_run`, `risk: "high"`) and asks for a
      token before the first one
- [ ] `union` keeps the bottom-most id (`plate`, now a `path`); `knob` is gone
- [ ] After `difference` the centre pixel is transparent and the plate stays navy around it
- [ ] `stroke_to_path`: `ring` becomes a path with fill `#f4a261`
- [ ] Turn 5: `combine` → one object; `break_apart` → several; undo via `restore` with the
      `break_apart` snapshot gives one object again (not a re-combine)
- [ ] `icon-paths.svg` contains only `<path>` elements, no `<circle>` / `<rect>`

### WF7 Foreign SVG and security

Fixture: `logo.svg`. Budget: ≈ 10 calls.

1. > Insert this badge into `logo.svg`:
   > `<g id="badge"><circle id="ins" cx="20" cy="20" r="8" fill="#00aa00" onload="alert(1)"/><script>alert(2)</script></g>`
2. > Also open `../../etc/passwd` for me.
3. > Replace the whole document with this SVG: `<svg xmlns="http://www.w3.org/2000/svg" width="10"
   > height="10"><rect id="only" width="10" height="10"/></svg>`.
4. > Undo that and save the result as `foreign.svg`.

- [ ] `insert_svg` / `set_svg` are HIGH: refused without a token, the agent asks
- [ ] With a token the fragment is still refused with `svg contains disallowed element: 'script'`
      / `svg contains a disallowed event-handler attribute: 'onload'` (not stripped); the agent
      removes the unsafe parts (or reports them) and nothing is applied until it does
- [ ] Turn 2: `path rejected: …`, no host path in the message; the agent does not try to get around it
- [ ] Turn 3 → 4: after `restore`, `bg`, `dot`, `label` are back
- [ ] `foreign.svg` is clean (contains neither `<script` nor `onload`)

### WF8 Long iteration loop

Fixture: `logo.svg`. Budget: ≈ 30 calls.

1. > Open `logo.svg`. We'll go in small steps: in each step move `dot` 5 px to the right and add a
   > small green square `step<n>` (10×10) next to the previous one at the top. Do 12 steps and show
   > me the picture after every 4th step.
2. > Step 3 was the best one. Go back to exactly that state.
3. > Continue from there with two more squares `step13` and `step14`.
4. > Clean up the history and check that the file is still valid.

- [ ] One `edit` per step; `history(action="list")` grows by exactly one per step
- [ ] Turn 2: the agent picks the right snapshot (the one **of step 4**, which holds the state after
      step 3): `step1`–`step3` exist, `step4` doesn't, `dot` moved 15 px in total
- [ ] Turn 3: the work continues on the restored state (`step12` stays gone)
- [ ] `prune` reports `retained`; `bg`, `dot`, `label` still exist; `validate.ok: true`

### WF9 Agent behaviour (path B only)

Fixture: `logo.svg`. Budget: ≈ 6 calls.

1. > Clean up `logo.svg`: delete everything except the text.
2. *(When asked for a token: "The token is `test-token`.")*
3. > Can you retouch the pixels in this photo for me?
4. > Wie mache ich die SVG fürs Web kleiner?

- [ ] Turn 1: the agent shows the plan first (`dry_run` or a list of the ids it will delete) and
      asks for an `approval_token` instead of inventing one
- [ ] After the token: **one** `edit` with all deletes (e.g. via `select`), then a look at the
      result; the agent mentions that `history(action="restore")` undoes it
- [ ] Turn 3: `help(goal=…)` returns `out_of_scope`; the agent declines and does not try anyway
- [ ] Turn 4: a non-English `help(goal)` finds `edit:optimize_web`

### Run 5, path B (2026-10-23, Claude Code, Haiku subagents at high effort, one per workflow)

Server reconnected after the run-4 fixes and the agent-guidance update; `INKSCAPE_MCP_RAW_ACTION_ENABLED=1`.
All source hashes unchanged except `wf3/poster.svg` (edited on purpose in turn 7).

| Workflow | Result | Calls | Budget | Note |
|---|---|---|---|---|
| WF1 | PASS | 12 | ≈ 10 | one `open` with ops + group; scale kept centre, width 65; overwrite: `dry_run`, token asked, `overwritten: true`, file has `#f4a261`; 5 snapshots, one per round. Extra `dry_run` + `inspect` on the scale |
| WF2 | PARTIAL | 19 | ≈ 10 | result correct: one `edit`, no `#e63946` left (CSS too; 0 in v2 and ship), `cta` centre kept, "NEW" in `layer-content`, layers + lock kept, aggressive = HIGH + token, no `namedview`, hash unchanged. Over budget: 3 `help` + ~6 `inspect`. `replace_color` also recoloured `band` in the locked layer without a note; `scale` on the `cta` rect leaves `cta-label` unscaled |
| WF3 | PARTIAL | 24 | ≈ 20 | still pre-corrected `brand-nam` before sending, but now told the user; no-op reported; delete: `dry_run` + token; turn 6 restored the turn-3 snapshot first (off by one), then fixed; reload / `pre_reload_snapshot_id` / `prune retained: 9` correct. One placeholder `doc_id` |
| WF4 | PARTIAL | 19 | ≈ 15 | one multi-doc `inspect` + `consistency`; sheet via `grid`, each variant keeps its accent (A red, B teal, C yellow); stale sheet noticed + rebuilt; set PNG names carry `doc_id`. First PNG export without `out_dir` landed in `.inkscape-mcp/documents/…`, re-exported |
| WF5 | PARTIAL | 16 | ≈ 10 | counts 10/30/80 in one edit (raised `max_matches` up front); fresh token per HIGH call; dry_run 30 then delete; `within_budget: false`, no files. Turn 3 restored the turn-1 snapshot (off by one) and redid turn 1 by hand. Non-dry-run results carry `needs_approval: true` |
| WF6 | PARTIAL | 25 | ≈ 12 | first real run: every path op `dry_run` + token (6 asks, none reused); `union` keeps `plate` as `path`; hole transparent (magenta bg render); `ring` path with fill `#f4a261`; combine 1 → break_apart 4 → restored the combine snapshot first (off by one), then the break_apart one → 1; `icon-paths.svg` only `<path>`. A dry_run per op doubles the calls |
| WF7 | PASS | 11 | ≈ 10 | unsafe fragment refused (`svg contains a disallowed event-handler attribute: 'onload'`) already at `dry_run`; agent stripped it and said so; new token asked for `set_svg` (no reuse); `path rejected: outside workspace; …`; restore brings back `bg`/`dot`/`label`; `foreign.svg` clean |
| WF8 | PARTIAL | 37 | ≈ 30 | all checks right (step 4's snapshot, `step12` gone, single `translate(25,0)`, `prune retained: 15`, `validate.ok`); over budget from a `history list` after each step plus one placeholder `doc_id`. `prune` that removes nothing gives no reason |
| WF9 | PASS | 7 | ≈ 6 | `dry_run` + token ask, one delete edit, undo named; out-of-scope declined; German goal passed verbatim → `edit:optimize_web`, reply in German |

Guidance effect vs run 4: token reuse gone (WF5/6/7 ask per call), German handled (WF9), id typo now reported but
still corrected before sending (WF3). New top gap: three agents (WF3, WF5, WF6) restored the snapshot of the edit
*before* the one to undo, although each edit result carries an `undo` hint with the right call.
