# Changelog

All notable changes to `inkscape-mcp` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

First public release. A Model Context Protocol (MCP) server that makes Inkscape / SVG documents
agent-ready over STDIO.

### Fixed
- **Windows: export / save / live sync no longer crash.** Directory creation used the POSIX-only
  `os.O_DIRECTORY` / `os.O_NOFOLLOW` / `dir_fd`, so every tool that creates an output directory
  (`export` incl. `save_as` and profiles, live sync to the workspace, ...) failed
  with `module 'os' has no attribute 'O_DIRECTORY'`. Platforms without `dir_fd` now use a path-based
  fallback that refuses symlinked / junctioned or escaping components (it checks after each
  `mkdir`, so a junction raced in mid-chain can leave at most one empty directory outside; files
  are still only written inside the sandbox); file writes add `O_BINARY` and drop `O_NOFOLLOW`
  only where it does not exist. A failing `mkdir` no longer leaks the host path in its error.

### Added
- **Ten result-oriented tools** — `open`, `inspect`, `edit`, `render`, `export`, `history`, `help`,
  `live`, `live_edit` and (advanced mode) `actions`. A drawing is `open(canvas, ops,
  render='inline')` → `export(targets)`: two calls instead of five to ten.
- **Typed ops in one atomic call** — `edit` / `open` apply up to 64 ops (create, style, object,
  transform, compose, path, optimize) as ONE all-or-nothing operation with one snapshot and one
  Operation Record. `"$<n>"` references ids created earlier in the same call; ops with targets take
  `ids` or a `select` ({tag, fill, stroke, text, id_prefix, bbox}); `dry_run` returns the plan; the
  risk of a call is the maximum over its ops.
- **Headless document lifecycle** — `open` (workspace file, SVG text, blank canvas or the live
  canvas) into tracked working copies keyed by an opaque `doc_id`; originals are never mutated.
- **Read & inspect** — `inspect` (summary, objects with selectors, tree, layers, styles, fonts,
  assets, validate, quality, snapshots, frames; one or several documents), plus MCP resources
  exposing document structure, the runtime capability matrix and the op reference (`inkscape://ops`).
- **Safe edits (medium risk, reversible)** — every mutation auto-snapshots and emits an Operation
  Record; `history(action='restore')` rolls back. Genuine no-ops write nothing (`changed: false`).
  `edit` / `open` results and dry-run plans report `locked_layers_touched` when objects in a locked
  layer change.
- **Render & export** — `render` and `edit(render='inline')` return an inline PNG; `export` writes
  files, web / icons / print profiles, `save_as`, for one document or a set, with dry-run size
  projection, a byte budget and in-process content-truth verification.
- **High-risk surfaces (approval-gated)** — path ops, overwrite-on-save, `delete`, `set_svg` /
  `insert_svg`, `live_edit` and Action chains; each requires a per-operation approval token.
- **Live mode** — `live` (observe selection / inspect / scene / view in one call, wait, viewport,
  diff, setup actions with auto-connect) and `live_edit` (apply / insert_svg / set_text) for a running
  Inkscape via a cross-platform transport abstraction (extension-socket bridge on any OS; DBus
  fast-path on Linux), gated by `INKSCAPE_MCP_LIVE_ENABLED`.
- **Discoverability** — `help(goal=…)` returns the `tool:op` to use plus an example call;
  `help(op=…)` / `help(topic='ops')` document every op; generated `llms.txt` / `llms-full.txt`
  (including the op reference); MCP tool annotations; a ≈15 KB `tools/list`; one-line, actionable
  error messages; a deterministic eval harness scoring `tool:op` targets.
- **Security model** — workspace sandbox with path + symlink guard, size / export / timeout limits,
  arg-list subprocess (never shell strings), safe XML parsing (no entity expansion), no network,
  no arbitrary extension execution, and risk-classed calls.

### Changed (realistic end-to-end workflows, `tests/e2e/`)
- `palette` / `replace_color` also rewrite colours inside `<style>` CSS rules when run
  document-wide (scoped runs leave shared CSS alone); `inspect(include=['styles'])` lists CSS-rule
  colours. Objects painted through a CSS class used to keep their old colour.
- `scale` and `rotate` (without `cx`/`cy`) pivot on the object's own centre instead of the parent
  origin, so objects stay in place; geometry the DOM cannot measure (e.g. text without x/y) falls
  back to the origin and the op summary says so.
- New objects go into the document's current layer (`inkscape:current-layer`), else the topmost
  layer, skipping locked and hidden layers (was: the first layer, often a locked background).
- `replace_text` keeps Inkscape's single-line `<tspan sodipodi:role="line">` and changes only its
  content.
- `optimize_web` keeps ids, layers, layer locks and the current layer by default (the document
  stays editable); `aggressive=true` is the final ship-only pass that also drops unreferenced ids,
  layer markers and the namedview. It is HIGH risk and needs an `approval_token`.
- `scale`/`rotate` pivots also work for curved and relative paths (all path commands are parsed).
- `move` defaults `dx`/`dy` to 0; a selector `bbox` also accepts `[x, y, width, height]`, the form
  `inspect` reports.

### Security
- The CSS colour scan uses possessive quantifiers (linear time); the first version backtracked
  cubically on whitespace in an untrusted `<style>` block. `<style>` text and path data over 1 MiB
  are not scanned.
- `optimize_web` finds references in every attribute, in `<style>` text (`url(#id)`, `#id`
  selectors), in both `href` forms and in SMIL `begin`/`end`, so it no longer prunes a gradient,
  marker or filter that is still in use.
- `scale` rejects factors that round to 0 (a singular transform); a selector `bbox` rejects
  negative or non-finite sizes.

### Fixed (scenario suite run 4)
- `grid` / `place` (and every deep copy) gave copies of a document with a `<style>` block shared,
  document-global class rules, so the last copy's `.accent` colour painted all of them. A copy's
  own stylesheet is now scoped to it: its classes get the copy's id suffix, and `#id` selectors
  follow the id remap. Classes defined outside the copy are untouched.
- `inspect` objects (and `created[].bbox`, selector `bbox` matching) report a box for elements
  with a `transform`: the attribute box goes through the element's own transform (`matrix`,
  `translate`, `scale`, `rotate`, `skewX/Y`), so "is it still centred after `scale`?" is
  answerable without the engine. Rotated/skewed circles and ellipses and unparseable transforms
  still report no box.

### Changed (scenario suite)
- `docs/test-scenarios.md` and `scripts/scenario_smoke.py` replace the single-feature K/R checks
  with eight long workflows WF1–WF8 (plus agent-only WF9): feedback rounds and changes after
  delivery, recovery from mistakes, variants and contact sheet, bulk selectors, path ops,
  foreign SVG and security, a 12-round iteration loop. The script enables path ops
  (`INKSCAPE_MCP_RAW_ACTION_ENABLED=1`) and runs DOM steps even without Inkscape. The doc is now
  in English.

### Fixed (agent scenario run 3, E21-15)
- The `sodipodi` namespace constant was wrong (`sodipodi-0.0.dtd` instead of Inkscape's
  `sodipodi-0.dtd`): `inspect` reported locked layers as unlocked, and `optimize_web` / `insert_svg`
  did not recognise real `sodipodi:*` attributes.
- `replace_color` / `palette` with `ids` or `select` now also recolour objects painted through a
  CSS class, by overriding the colour inline on those objects; the shared `<style>` rule and other
  objects using it stay unchanged (was a silent `changed: false`).
- `help(goal=…)` understands common German phrasings ("kleiner fürs Web", "rückgängig", …).
- Error messages name the next step: `path rejected: outside workspace; pass a path relative to
  the workspace root`, `opened <doc_id>, but ops failed: …; fix it and call edit(doc_id=…)`,
  `requires object_id; call inspect(…)`, an unknown id in a small document lists the ids, and
  `save_as` with `overwrite=True` but no token says the token is what is missing.
- `inspect(detail='full')` shows an object's `transform`; `save_as` reports the written file's
  `bytes` (and counts in `count` / `total_bytes`, was `0`); `export(dry_run=true)` says its
  `projected_bytes` is an upper bound; the HIGH-risk refusal points to `dry_run=true` and
  `history(action='restore')`.

### Changed (agent scenario re-run, E21-07)
- `font` takes a numeric `size` (`8`) as well as a CSS length string (`"8pt"`).
- "document id not found" adds "(use the doc_id open returned, not a path)": 4 of 19 agents passed
  the file path as `doc_id`.
- `export` / `save_as` and `render(target='object')` descriptions say that `dest_path` is
  workspace-relative (`out_dir` does not apply) and that an object render is a zoomed view.

### Changed (agent scenario run, Weg B in `docs/test-scenarios.md`)
- `open(..., render='inline')` without `ops` returns a picture of the opened page instead of an
  error (6 of 19 agent runs tripped over the old error).
- The `edit` `ops` field lists every op with its fields (`create_text(x y text fill? … anchor?)`),
  so an agent can write a call without a `help(op=…)` round trip first (+≈1.3 KB on `edit`).
- A HIGH-risk edit that changed the document returns `undo` with the exact
  `history(action='restore', snapshot_id=…)` call.
- `history(action='list')` returns `count` (also `0`, where the empty list used to vanish).
- `create_text` takes `anchor` (`start` / `middle` / `end`) to centre or right-align text on `x`.

### Changed (breaking relative to the pre-release 99-tool surface)
- The 99 single-purpose tools (`create_rect`, `set_fill`, `apply_edits`, `export_document`,
  `live_session_step`, …) are **gone**; there is no legacy profile and no compatibility layer. Their
  behaviour lives on as ops of `edit` (`create_rect`, `fill`, …), actions of `history` / `live` /
  `live_edit` / `actions`, sections of `inspect`, and targets of `export`. `tools/list` shrank from
  ≈325 KB to ≈15 KB (87 → 10 tools).
- `how_do_i`, `list_capabilities`, `diagnose_runtime` and `stat_artifact(s)` are folded into `help`
  (`goal`, `topic='capabilities'|'diagnose'`, `stat_paths`).
- Edits no longer render before/after PNGs by default (0 Inkscape renders per edit instead of 2);
  ask for a picture with `render='inline'`, or opt in to preview frames on the Operation Record with
  `INKSCAPE_MCP_EDIT_PREVIEWS=after|before_after`.
- Tool results are one compact JSON text block (plus an inline image where asked) with no
  `outputSchema`.
- `delete` on an unknown id is an error (it no longer skips silently), and a call with any failing
  op changes nothing.
- The `core` tool profile and `short` description mode (`INKSCAPE_MCP_TOOL_PROFILE`,
  `INKSCAPE_MCP_TOOL_DESC`) are removed; visibility is controlled only by `INKSCAPE_MCP_LIVE_ENABLED`
  and `INKSCAPE_MCP_RAW_ACTION_ENABLED`. The `run_raw_action` tool is now `actions(action='raw')`.

[0.1.0]: https://github.com/jjjsood/inkscape-mcp/releases/tag/v0.1.0
