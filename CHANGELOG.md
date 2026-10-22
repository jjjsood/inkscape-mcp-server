# Changelog

All notable changes to `inkscape-mcp` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

First public release. A Model Context Protocol (MCP) server that makes Inkscape / SVG documents
agent-ready over STDIO.

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
