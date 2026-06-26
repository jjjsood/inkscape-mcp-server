# inkscape-mcp

> A Model Context Protocol (MCP) server that makes Inkscape / SVG documents **agent-ready** —
> inspect, edit safely, validate, render, and export vector graphics from any MCP client.

[![Python](https://img.shields.io/badge/python-%E2%89%A53.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastMCP](https://img.shields.io/badge/MCP-FastMCP%203.x-5A45FF)](https://github.com/jlowin/fastmcp)
[![Transport](https://img.shields.io/badge/transport-STDIO-444)](#connecting-an-mcp-client)
[![License](https://img.shields.io/badge/license-MIT-green)](#license)

`inkscape-mcp` exposes a small, strongly-typed tool surface over your SVG documents. An LLM agent
can open a drawing, read its structure, recolour and re-letter objects, transform geometry, render
previews, and export production assets — all **headless-first** and **reversible by construction**.
Every mutating operation runs on a working copy, takes a snapshot, and records an Operation Record,
so nothing the agent does touches your originals or can't be undone.

---

## Table of contents

- [Why](#why)
- [Highlights](#highlights)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Install & quickstart](#install--quickstart)
- [Connecting an MCP client](#connecting-an-mcp-client)
- [Configuration](#configuration)
- [Tool reference](#tool-reference)
- [Resource reference](#resource-reference)
- [Prompt reference](#prompt-reference)
- [Try asking your agent to…](#try-asking-your-agent-to)
- [Safety model](#safety-model)
- [Project layout](#project-layout)
- [Development](#development)
- [Roadmap](#roadmap)
- [License](#license)

---

## Why

LLM agents are good at reasoning about *what* should change in a drawing ("make the logo blue,
bump the heading to 24 px, export a 512 px icon") but bad at safely poking at raw XML or driving a
GUI. A naive "run this Inkscape command" tool is dangerous: it can overwrite originals, shell out
unsafely, or silently corrupt a file with no way back.

`inkscape-mcp` solves that with a **bounded, typed API**: each capability is its own small tool
with explicit parameters and a declared risk class. Simple structural edits go through a direct
`lxml` DOM layer; rendering, export, and complex geometry go through the Inkscape CLI. Originals
are never mutated, every change is snapshot-backed and reversible, and subprocess calls use
argument lists — never shell strings.

## Highlights

- **88 typed tools** across read, validate, render, export, optimize, safe-edit, element-creation,
  defs/grouping, path-geometry, snapshot, save, and live groups.
- **Headless-first.** No GUI required; the Inkscape binary is used only for render / export /
  geometry, and the server probes the runtime instead of assuming a version.
- **Reversible by construction.** Every mutating op = pre-mutation snapshot + before/after preview
  + Operation Record. `restore_snapshot` rolls back.
- **Originals are sacred.** Documents open into tracked working copies. Nothing writes over the
  source file; saving goes to a *new* path, and overwrites are gated behind explicit approval.
- **Risk-classed.** Each tool declares `low` / `medium` / `high` / `restricted`; the policy layer
  enforces it (high-risk needs a per-operation approval token; restricted never ships in the MVP).
- **Sandboxed.** Workspace-root jail, path normalization + symlink guard, input/output/export size
  limits, per-process timeouts, safe XML parsing (no entity expansion), no network, no arbitrary
  extension execution.
- **MCP resources** expose document structure (summary / tree / layers / objects / styles / fonts /
  assets) and the runtime capability matrix as addressable URIs.

## How it works

```
MCP client (Claude, etc.)
        │  STDIO / JSON-RPC
        ▼
   FastMCP app  ──►  typed @mcp.tool functions  (risk-classed, validated args)
        │
        ├─ direct-DOM engine (lxml)        → structure read + safe edits
        ├─ Inkscape CLI adapter (arg-list) → render / export / geometry
        ├─ snapshot engine                 → pre-mutation copy + reversible restore
        ├─ Operation Records               → audit trail of every mutation
        └─ workspace sandbox               → path/size/timeout/XML safety
```

1. **`open_document`** copies your SVG into a tracked workspace document and hands back an opaque
   `doc_id`. The original file is never opened for writing again.
2. **Read tools / resources** inspect that working copy — tree, layers, styles, fonts, assets.
3. **Edit tools** mutate the working copy through the pipeline: take a snapshot → apply the change
   → render a before/after preview → write an Operation Record. All medium-risk and reversible.
4. **Render / export tools** shell out to the Inkscape CLI (argument lists only) and drop artifacts
   into the workspace artifacts / exports directories as workspace-relative paths.
5. **`save_document_as`** writes the working copy to a *new* file (validated before and after).
   Overwriting an existing file is a separate, approval-gated high-risk path.

## Requirements

- **Python ≥ 3.12** and [`uv`](https://docs.astral.sh/uv/).
- Runtime deps (installed by `uv sync`): **FastMCP 3.x**, **lxml**, and **Pillow** (the focused
  live before/after visual diff, `live_diff_view`, uses Pillow to pixel-diff frames and draw the
  annotation overlay).
- **Inkscape on `PATH`** for the render / export / geometry tools (developed and tested against
  Inkscape **1.4.x**). Read / edit / validate tools work without it.
  - Probe your install with `inkscape --version` / `inkscape --action-list`, or run the
    `diagnose_runtime` tool.
- At least one **workspace root** configured (see [Configuration](#configuration)) — the sandbox
  refuses to touch anything outside it.

## Install & quickstart

The package exposes one console script, `inkscape-mcp` → `inkscape_mcp.server:main`, which starts the
FastMCP app over STDIO. Install it via `uvx` (one-shot), `pipx` (persistent), or from source.

> **Not on PyPI yet** — install from source / git (same package, same script). Bare-name
> `uvx inkscape-mcp` / `pipx install inkscape-mcp` will work once published.

```bash
# uvx — run without installing (from a local checkout; the repo root holds pyproject.toml):
uvx --from /abs/path/to/inkscape-mcp inkscape-mcp

# uvx — straight from git:
uvx --from "git+https://github.com/johnnyjagatpal/inkscape-mcp.git" inkscape-mcp

# pipx — persistent install of the console script:
pipx install /abs/path/to/inkscape-mcp

# from source (development / dogfooding):
cd inkscape-mcp           # the repo root
uv sync                 # install runtime + dev dependencies
uv run pytest           # run the test suite
uv run inkscape-mcp     # start the STDIO MCP server
```

The launched server waits on stdin for MCP JSON-RPC (an MCP host drives it). Confirm it boots with
`inkscape-mcp </dev/null` or `uv run python -c "from inkscape_mcp.server import main"`. Full install
matrix (incl. the `claude mcp add` form): [`docs/install/install.md`](docs/install/install.md).

Quality gates:

```bash
uv run ruff check --fix .
uv run ruff format .
uv run mypy src
```

> Tests that need a real Inkscape binary are marked `@pytest.mark.inkscape`. They **auto-skip**
> when no `inkscape` is on `PATH` (central `pytest_collection_modifyitems` hook in
> `tests/conftest.py`), so the suite is green on a host without Inkscape; they run normally when the
> binary is present. Force-skip explicitly with `uv run pytest -m "not inkscape"`.

**CI.** [`.github/workflows/ci.yml`](.github/workflows/ci.yml) (E7-02) runs ruff + ruff-format
+ mypy + pytest on Linux/macOS/Windows (headless + the cross-platform live-transport suite), the
full suite incl. real-Inkscape tests on Linux, and a packaged `pipx`-install STDIO boot smoke on all
three OSes, plus a full-surface MCP smoke (`ci_surface_smoke.py`, E9-03) that asserts the registered
primitive counts (**99 tools / 7 prompts / 16 resources**) and reads every resource over an in-memory
client. CI helper scripts live in [`scripts/`](scripts/) (`ci_diagnostics.py`, `ci_boot_smoke.py`,
`ci_surface_smoke.py`).

**Evals.** [`evals/`](evals/) holds a deterministic, CI-runnable tool-usability harness (E15-06) that
makes "agent-friendly" measurable without a live LLM: `tool_selection_scenarios.json` is a labelled
set of natural-language asks (mirroring the intent catalog below), and `run_eval.py` drives the
server's own discovery layer (`how_do_i` / [`intents.py`](src/inkscape_mcp/intents.py)) to report
per-group + overall tool-selection accuracy and out-of-scope flagging (`uv run python
evals/run_eval.py`, `--json` for the report dict). `tests/test_eval_harness.py` gates it against
regressions; the scenario schema is runner-agnostic. An OPTIONAL, report-only live-agent runner
(`run_live_eval.py`, E15-06a) reuses the SAME dataset + scorer to capture real tool-call traces +
turn count via a pluggable `AgentDriver` (deterministic `ReplayDriver` by default; the real MCP+LLM
path is off unless `--driver real` / `INKSCAPE_MCP_EVAL_DRIVER=real`) and never gates CI.

## Connecting an MCP client

The server speaks **MCP over STDIO**. Point any MCP-capable client at the `inkscape-mcp` console
script. Ready-to-copy host configs live in [`examples/`](examples/)
([`claude_desktop_config.json`](examples/claude_desktop_config.json),
[`mcp.json`](examples/mcp.json)); full per-host instructions (Claude Desktop, Claude Code +
`claude mcp add`, generic STDIO) are in
[`docs/install/host-configs.md`](docs/install/host-configs.md). Example client config:

```jsonc
{
  "mcpServers": {
    "inkscape": {
      "command": "uvx",
      "args": ["--from", "/absolute/path/to/inkscape-mcp", "inkscape-mcp"],
      "env": {
        "INKSCAPE_MCP_WORKSPACE_ROOTS": "/absolute/path/to/your/svgs"
      }
    }
  }
}
```

(With a `pipx install`, use `"command": "inkscape-mcp"`, `"args": []`.) To dogfood straight from a
source checkout, point a host at `uv run --directory /abs/path/to/inkscape-mcp inkscape-mcp` once
`uv sync` has run. Map any platform/feature gap with the
[compatibility matrix](docs/install/compatibility.md) and
[troubleshooting](docs/install/troubleshooting.md) docs.

## Configuration

All configuration is environment-driven (no config file needed). The sandbox is the only **required**
setting — without a workspace root the server has nothing it is allowed to touch. The same table (and
the `claude mcp add` / generic-host forms) is in
[`docs/install/host-configs.md`](docs/install/host-configs.md).

| Env var | Default | Purpose |
|---|---|---|
| `INKSCAPE_MCP_WORKSPACE_ROOTS` | *(none)* | **Required.** OS-path-separated list of directories the server may read/write. Everything else is rejected. |
| `INKSCAPE_MCP_MAX_INPUT_BYTES` | `52428800` (50 MiB) | Max size of an input SVG. |
| `INKSCAPE_MCP_MAX_EXPORT_PX` | `8192` | Max raster dimension for render/export. |
| `INKSCAPE_MCP_MAX_OUTPUT_BYTES` | `104857600` (100 MiB) | Max size of a produced artifact. |
| `INKSCAPE_MCP_PROCESS_TIMEOUT_S` | `60` | Per-Inkscape-process timeout (seconds). |
| `INKSCAPE_MCP_MAX_PROCS` | `2` | Max concurrent Inkscape subprocesses. |
| `INKSCAPE_MCP_SNAPSHOT_KEEP_N` | `50` | Snapshots retained per document. |
| `INKSCAPE_MCP_SNAPSHOT_KEEP_DAYS` | `30` | Snapshot age retention. |
| `INKSCAPE_MCP_SNAPSHOT_HARD_MAX_N` | `500` | Hard cap on snapshots per document. |
| `INKSCAPE_MCP_SNAPSHOT_HARD_MAX_BYTES` | `5368709120` (5 GiB) | Hard cap on snapshot bytes. |
| `INKSCAPE_MCP_ARTIFACT_KEEP_DAYS` | `14` | Artifact age retention. |
| `INKSCAPE_MCP_ARTIFACT_MAX_BYTES` | `2147483648` (2 GiB) | Total artifact byte budget. |
| `INKSCAPE_MCP_ARTIFACT_MAX_BYTES_PER_DOC` | `536870912` (512 MiB) | Per-document artifact byte budget. |
| `INKSCAPE_MCP_LIVE_CACHE_MAX_ENTRIES` | `64` | Max frames in the per-session live render cache (E8-06; LRU). Floored. |
| `INKSCAPE_MCP_LIVE_CACHE_MAX_BYTES` | `268435456` (256 MiB) | Total byte budget for the live render cache (E8-06; LRU eviction). Floored. |
| `INKSCAPE_MCP_LIVE_COALESCE_BUDGET_MS` | `200` | Frame-coalescing latency budget (E8-06): a repeated identical-key render within this window returns the just-cached frame instead of re-rendering. `0` disables. |
| `INKSCAPE_MCP_LIVE_FRAME_KEEP_DAYS` | `7` | Age retention for loop/live render frames (E8-06), pruned by the **explicit** retention sweep (boot + `prune_snapshots`), never implicitly by a mutating tool. |
| `INKSCAPE_MCP_LIVE_FRAME_MAX_BYTES` | `536870912` (512 MiB) | Total byte budget for loop/live render frames (E8-06; newest kept), pruned by the explicit sweep. |
| `INKSCAPE_MCP_LIVE_ENABLED` | `true` | Master gate for live mode (E3). **On by default** (operator-chosen); set a falsy value (`0`/`false`/`no`/`off`) to opt out, in which case `live_connect` refuses cleanly. |
| `INKSCAPE_MCP_LIVE_RENDEZVOUS` | *(none)* | Optional explicit path to the live helper's rendezvous file (otherwise discovered under the Inkscape user data dir / temp dir). |
| `INKSCAPE_MCP_ACTION_ALLOWLIST` | *(built-in defaults)* | OS-path-separated list of Inkscape Action ids **added** to the built-in allowlist (E6-02). Server-side, never client-supplied; cannot remove a default or open arbitrary passthrough. Each Action must also exist in the version-keyed capability map to run. |
| `INKSCAPE_MCP_EXTENSION_ALLOWLIST` | *(empty)* | OS-path-separated list of Inkscape extension ids added to the (empty) execution allowlist (E6-02). Discovery is read-only and unaffected. |
| `INKSCAPE_MCP_RAW_ACTION_ENABLED` | `false` | Advanced-mode gate for the `run_raw_action` escape hatch (E6-03 / ADR-003). **Off by default**; set a truthy value (`1`/`true`/`yes`/`on`) to opt in. Enabling it does **not** widen the allowlist — every Action still has to be allowlisted, present in the capability map, charset-safe, and (for a real run) HIGH + approval-token-gated. |
| `INKSCAPE_MCP_ENGINE_MODE` | `per_call` | Engine transport for render/export/path/boolean/action-chain (E12 / ADR-007). `per_call` spawns a fresh Inkscape per call (default, always correct); `shell` routes those ops through one warm, long-lived `inkscape --shell` worker per document with an **automatic per-call fallback** on any fault. Faster for multi-op batches; any value other than `shell` floors to `per_call`. A private headless worker, **not** a channel to your live GUI. |
| `INKSCAPE_MCP_ENGINE_MAX_PROCESSES` | `2` | Max concurrent warm shell workers when `engine_mode=shell` (LRU-evicted). Floored at 1. |
| `INKSCAPE_MCP_ENGINE_IDLE_TIMEOUT_S` | `300` | Seconds before an idle warm shell worker is reaped. Floored at 1. |
| `INKSCAPE_MCP_TOOL_PROFILE` | `full` | Tool-disclosure profile (E18-03). `full` leaves the flag-allowed surface unchanged; `core` narrows `tools/list` to the curated essential authoring set (`document`/`find`/`create`/`style`/`transform`/`export`/`snapshots` modules) to cut per-turn model-context cost (~60% fewer `tools/list` tokens). Only **narrows** within the flag-allowed surface (sec.12 / ADR-003); a stray value floors to `full`. |
| `INKSCAPE_MCP_TOOL_DESC` | `full` | Tool-DESCRIPTION mode (E20-03), orthogonal to `INKSCAPE_MCP_TOOL_PROFILE` (which trims tool COUNT — this trims description LENGTH). `full` serves each tool's complete docstring; `short` serves a DERIVED short form (the first "what it does" line + the `Risk class:` line), cutting ~86% of the description bytes (~23k tokens) off every `tools/list`. The short form is always derived from the canonical docstring (no second copy) and the JSON `inputSchema` (param names/types) is untouched, so callers keep full argument detail; `llms.txt` / `llms-full.txt` always carry the full catalog. A stray value floors to `full`. |

## Tool reference

89 tools. Risk classes: **low** (read / render / export / quality / Action discovery) · **medium**
(write-new / element-creation / defs-grouping / style / text / transform / web-optimize / typed batch; reversible)
· **high** (overwrite / delete / path geometry / Action chains / raw Action; approval-gated) ·
**restricted** (live helper install).
Mutating tools return an `EditResult` carrying the `operation_id`, snapshot id, and a before/after
preview.

**Conventions (param + path naming).** Single-object tools take `object_id`; multi-object tools take
`object_ids`. A caller-chosen write target is `dest_path` (a file) or `out_dir` + `name_prefix` (a
directory); a relative `dest_path`/`out_dir` anchors to the **workspace root**, never the process
CWD, and is sandbox + symlink checked (`path rejected: outside workspace` otherwise). Every
artifact-producing tool returns a `workspace_relative_path` (root-relative, opens directly with no
`find`/`stat`) alongside the managed `artifact_path`; no absolute host path ever appears in a result
(sec.12). `changed` on a mutating result is decided in ONE place — the edit pipeline canonical-serializes
the document before and after the mutation. A real change reports `changed: true` with a linked snapshot +
Operation Record; a genuine **no-op** (e.g. `replace_color` matching nothing, `normalize_viewbox` on a
valid viewBox, `set_fill` to the colour already present, a second `fit_to_content`) reports
`changed: false`, empty `operation_id`/`snapshot_id`, and writes **no snapshot and no Operation Record** —
nothing happened, so nothing clutters the snapshot list or the audit trail.

**MCP `ToolAnnotations` (E17-01).** Every tool also carries machine-readable MCP annotations —
`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`, and a human `title` — derived
from ONE central map (`src/inkscape_mcp/tool_annotations.py`) keyed off the tool's existing risk
class, applied as a post-registration pass at boot. `readOnlyHint` follows the risk class directly
(low ⇒ read-only); destructive (overwrite/delete/outline), idempotent (pure re-set), and open-world
(host probes + `live_*`) sets are explicit in that one module. A client reads read-vs-write,
destructiveness, and idempotency without parsing docstring prose; titles are static labels only (no
host path, sec.12). Adding a tool with a `Risk class:` docstring line auto-annotates it.

**Tags + progressive disclosure (E17-02).** Every tool also carries exactly one **domain** tag —
`create` / `edit` / `transform` / `paths` / `export` / `live` / `actions` / `system` / `quality` —
and one **risk** tag (`low` / `medium` / `high` / `restricted`), stamped from ONE central map
(`src/inkscape_mcp/tool_tags.py`) by the same boot pass. The two EXISTING operator flags then drive
**tag-based exclusion** so a default client sees a smaller core surface and opts into the advanced /
live groups (FastMCP `disable(tags=…)` visibility transforms — they only NARROW `tools/list`, never
widen it):

| Flag | Default | Off → hides |
|------|---------|-------------|
| `INKSCAPE_MCP_LIVE_ENABLED` | `true` | every `live`-tagged tool |
| `INKSCAPE_MCP_RAW_ACTION_ENABLED` (advanced mode) | `false` | the ADR-003 hatch group: `run_raw_action` + every `paths`- and `actions`-tagged tool |

So the **default** surface (live on, advanced off) exposes the core 86 tools; turn advanced mode on
to add the `paths`/`actions` geometry + Action surface (full 98), or turn live off to drop the live
group (66 with both off). The self-describing `list_capabilities.tool_count` / `tools[]` (E16-01)
report the **active** post-filter surface, since they read the same `mcp.list_tools()` the transforms
filter. The generated `llms.txt` manifest still documents the FULL catalog (generated with both flags
forced on).

**Minimal `core` profile (E18-03).** For a still-smaller default model-context footprint, the opt-in
`INKSCAPE_MCP_TOOL_PROFILE` env (`full` default · `core`) narrows `tools/list` further to a curated
essential authoring set — the `document` / `find` / `create` / `style` / `transform` / `export` /
`snapshots` modules (open/inspect/find/create-*/style/transform/export/snapshot). Spike finding: the
default 85-tool surface is ~76k tokens of `tools/list` every turn; the 40-tool core set is ~31k —
a ~60% per-turn saving. The profile only **NARROWS** within the flag-allowed surface (it disables the
non-core tools by name; it can never expose a tool the live/advanced flags hide — sec.12 / ADR-003),
reuses the same `disable(...)` machinery, and is idempotent + re-evaluatable. A stray value floors to
`full`. `tool_count` / `tools[]` report the active surface; `llms.txt` still documents the FULL
catalog (generated with profile `full`). Everything outside core stays reachable by selecting `full`.

### System & diagnostics — *low*

| Tool | Signature | Description |
|---|---|---|
| `diagnose_runtime` | `()` | Probe the local Inkscape + Python runtime **fresh** and return the capability matrix (version, actions, export formats, DBus/live, inkex, fonts) — plus the curated `intents` goal→tool map and the authoritative MCP tool surface (`tool_count` + `tools:[{name, purpose, risk}]`, from the live registry — E16-01). |
| `list_capabilities` | `()` | Return the **cached** capability matrix (probed once, then reused). Includes an additive `intents` section: the curated natural-language goal → tool(s) map (`[{goal_pattern, tools, how_to, group}]`) — the same map `how_do_i` matches against. Also carries the authoritative MCP tool surface: `tool_count` (one true count of registered `@mcp.tool`s) + `tools:[{name, purpose, risk}]`, sourced from the live registry (E16-01) — one number instead of four. |
| `how_do_i` | `(goal)` | Map a natural-language goal to the concrete tool name(s) that achieve it (best match first: `[{goal_pattern, tools, how_to, group}]`). **Guidance only — executes nothing** (ADR-002/003: no portmanteau / raw-action hatch). Flags out-of-scope goals (raster/pixel edit, arbitrary Action/extension/script, network fetch, code exec) with `out_of_scope=True` + a reason; suggests `list_capabilities`/`inspect_document` on no match. *Low (no snapshot/Operation Record).* |
| `stat_artifact` | `(path)` | Read-only on-disk size + sha256 of one sandboxed artifact → `{path, bytes, sha256}` (E16-06). Path is workspace-relative or absolute, sandbox+symlink validated (escape → `path rejected: outside workspace`); size-capped (`max_input_bytes`), sha256 streamed in chunks; echoed `path` is workspace-relative (no host-path leak). Replaces a `wc -c`/`sha256sum` fallback. |
| `stat_artifacts` | `(paths)` | Set variant of `stat_artifact` → `{artifacts:[{path, bytes, sha256}], total_bytes, count}` (E16-06). Per-file stat + aggregate byte budget for an icon set / `dist/` tree in one call; same per-path sandbox + size rules. |

### Document — *low (create is medium)*

| Tool | Signature | Description |
|---|---|---|
| `open_document` | `(path)` | Open an SVG into a tracked workspace working copy; returns an opaque `doc_id` plus summary. `path` may be workspace-**relative** (anchored to the workspace root, not the process CWD) or absolute; either is sandbox + symlink checked (`path rejected: outside workspace` otherwise). Original is never mutated. Docstring documents the working-copy model. |
| `create_document` | `(width, height, viewBox?, background?)` | Create a blank, tracked working-copy document from scratch — no source file required. `validate_document`-clean; returns the same `{doc_id, summary}` shape as `open_document`. Optional validated `background` colour painted as a full-page rect. *Medium, reversible downstream.* |
| `reload_document` | `(doc_id)` | Refresh a working copy from its source under the **same** `doc_id`: takes a pre-reload snapshot (reversible), re-validates the source is still in the sandbox, re-copies it over the working copy. A `create_document` doc restores from its blank seed. Returns the refreshed summary + `pre_reload_snapshot_id`. |
| `inspect_document` | `(doc_id)` | Aggregate inspection: tree, layers, styles, fonts, external assets, and an addressable `objects` list (`ObjectRef`: `object_id`/`tag`/`bbox`/`fill`/`stroke`/`text`). Each element carries a `paint` summary (`fill`/`stroke`/`stroke-width`) + `is_leaf`/`is_layer`; objects carry `bbox`; fonts/assets flag `available` and `used_by`. |
| `find_objects` | `(doc_id, tag?, fill?, stroke?, text?, id_prefix?, bbox?, accurate_bbox?)` | Read-only filter over a document's addressable objects (AND semantics) → `[{object_id, tag, bbox?, fill?, stroke?, text?}]`. Paint matched casing-/hex-shorthand-insensitive (`dom.color_key`) and resolved through the CSS cascade — a `<style>` rule / `.class` / `#id` / inherited paint matches a `fill`/`stroke` filter (reported tokens stay per-element). `bbox` = attribute-derived box intersection (path/text/group/transformed excluded under a bbox filter) **unless** `accurate_bbox=true`, which opts into a single batched `inkscape --query-all` for true outline/transform-aware boxes (degrades to the attribute box if the engine is absent); `text` = case-insensitive substring. Makes id-taking edit tools usable on documents the agent did not author. *Low (read-only; `accurate_bbox` runs the engine).* |

### Compose / adopt SVG — *high (approval-gated), reversible*

| Tool | Signature | Description |
|---|---|---|
| `set_document_svg` | `(doc_id, svg, approval_token?)` | Replace the whole working copy with an agent-composed SVG string (root must be `<svg>`). Hardened safe-parse + strict element/attribute allowlist (rejects `<script>`, `on*` handlers, `javascript:`/external/`data:` hrefs — only same-document `#id` refs allowed). Auto-runs `validate_document` and folds findings into the result (`validation`). Reversible via the pre-mutation snapshot. |
| `insert_svg_fragment` | `(doc_id, svg, parent_id?, unwrap?, approval_token?)` | Insert an agent-composed SVG fragment (one element subtree) under `parent_id` (must exist) or the document root. A wrapper `<svg>` is unwrapped by default (`unwrap=true`); pass `unwrap=false` to keep an explicit nested `<svg>` container as-is. Same hardening + inline `validation` as `set_document_svg`. Reversible. Closes the `Write`→re-`open_document` loop. |
| `compose_grid` *(medium)* | `(rows, cols, cell, doc_ids? \| object_ids?+source_doc_id?, target_doc_id?, gap?, padding?, scale_to_fit?)` | Lay out N **different** assets in a `rows`×`cols` grid (contact/spec sheet) in ONE reversible call (E16-05). EXACTLY ONE source mode: `doc_ids` (one whole doc per cell) or `object_ids`+`source_doc_id` (objects from one doc). Each asset is deep-copied + re-id'd and wrapped in a `<g>` cell group translated to its row-major origin + optionally DOWN-scaled to fit `cell − 2·padding`. Composes into `target_doc_id` or creates a blank sheet sized to the grid. One snapshot + Operation Record for the whole sheet (ADR-004); sources never mutated. Reuses `tile`'s placement primitives. |
| `place_document` *(medium)* | `(target_doc_id, x, y, source_doc_id?, object_id?, scale=1.0)` | Place an existing document OR one named object INTO another document at `(x, y)` with `scale` (E16-10a) — the single-asset companion of `compose_grid`. The source subtree is deep-copied + re-id'd (sources never mutated) and wrapped in a `<g>` translated to `(x, y)` and uniformly scaled, under one snapshot + Operation Record. Lets existing geometry be re-composed cross-doc without re-authoring. |

### Validation — *low*

| Tool | Signature | Description |
|---|---|---|
| `validate_document` | `(doc_id)` | Validate a loaded document; returns structured, machine-readable findings. Includes a per-text-element glyph-coverage check (`missing_glyphs`): when the declared font's OWN cmap (read via fontconfig, not auto-substitution) cannot render the text, it names the uncovered characters and a covering family to try. |
| `quality_report` | `(doc_id)` | Machine-readable quality report: wraps the `validate_document` findings and adds metrics (object/node/layer counts, embedded-raster weight, font coverage, viewBox health) plus optimization opportunities consistent with what `svg_web_optimize` strips. Read-only. |
| `quality_report_set` | `(doc_ids)` | Quality-report a **set** in one read-only call (E16-05): per-doc `QualityReport` + aggregate (`all_ok`, `worst_score`/`mean_score`, `total_opportunities`) + a structured cross-doc `consistency` verdict (per property viewBox/stroke-width/id-naming: `agree` + `majority` + `{value:[doc_ids]}` + unknowns). Composes the per-doc engine; no snapshot. |

### Render & export — *low*

Every render/export result carries a `stale: bool` staleness signal (E14-06): `False` on a freshly
produced artifact (it reflects the current working copy); reserved to flag a previously-returned
artifact that the working copy has since outgrown (full mtime tracking is a follow-up — see E14-06a).

Render/export results also self-certify content truth (E16-07), computed in-process at produce time
(no `pdffonts`/`pdfimages`/`mutool` shell-out, no Pillow subprocess): a **raster** (PNG) result carries
`opaque_px` (drawn non-transparent pixel count) + `all_blank` so "the render actually drew something"
is checkable from the result; a **PDF** result carries `is_vector` (no embedded raster image) +
`fonts_outlined` (no embedded font — text outlined to paths), true vector when both hold. Each field is
additive and `None` for outputs it does not apply to (or when verification was skipped).

| Tool | Signature | Description |
|---|---|---|
| `render_preview` | `(doc_id, width_px?, name?)` | Render a PNG preview of the whole document into the artifacts dir. Successive calls at the same width never clobber (unique frame per call; optional `name`/tag). Reports the true on-disk raster size + content-truth `opaque_px`/`all_blank` (E16-07 — prove the render drew pixels). |
| `export_document` | `(doc_id, format, width_px?, out_dir?, name_prefix?)` | Export the whole document to **PNG / PDF / SVG** in the exports dir (or a sandbox-checked `out_dir`). Reports the true written raster size for PNG, plus content-truth (E16-07): PNG → `opaque_px`/`all_blank`, PDF → `is_vector`/`fonts_outlined` (true vector when both hold). |
| `export_object` | `(doc_id, object_id, format="png", width_px?, out_dir?, name_prefix?)` | Export a single object (by id) clipped to its bounding box; reports the actual clipped raster size + the same content-truth fields (E16-07). The id is charset-validated and never passed raw to Inkscape. |
| `capture_frame` | `(doc_id, series?, width_px?, label?)` | Capture the next numbered PNG screenshot in a per-run frame series (`frame-001.png`, `frame-002.png`, …) under `artifacts/frames/<series>/`, to document a scripted edit run. Canvas only (no UI chrome). The index is filesystem-derived (monotonic, restart-proof, never clobbers); `series`/`label` are sanitized to a single managed sub-dir. Returns the path plus `series`/`frame_index`. |
| `list_frames` | `(doc_id, series?)` | List the frames of a `capture_frame` series ordered by index (resolvable workspace-relative paths). Empty when the series is unused. Read-only. |

### Export profiles & batch — *low*

| Tool | Signature | Description |
|---|---|---|
| `export_web_profile` | `(doc_id, width_px=1024, widths?, scales?, out_dir?, name_prefix?)` | Web asset set: one PNG raster plus one plain SVG. Pass `widths`/`scales` for a 1×/2×/3× responsive PNG set in one call (each output distinct + resolvable). `out_dir`/`name_prefix` (E16-03) write a caller-named tree (e.g. `dist/web/`) directly — relative anchors to the workspace root, sandbox-checked. PNG entries report `opaque_px`/`all_blank` (E16-07). |
| `create_icon_set` | `(doc_id, sizes?, out_dir?, name_prefix?)` | Multi-size square PNG icon set from the source document. Over-cap and ≤0 sizes give distinct error messages. `out_dir`/`name_prefix` (E16-03) target a caller-chosen dir (sandbox-checked). Entries report `opaque_px`/`all_blank` (E16-07). |
| `export_print_profile` | `(doc_id, out_dir?, name_prefix?)` | Print-oriented vector PDF of the whole document; applies and **reports** print-specific export settings (so output differs from a plain PDF and is auditable). `out_dir`/`name_prefix` (E16-03) write the verified PDF straight into the `dist/` tree (sandbox-checked). Reports content-truth `is_vector`/`fonts_outlined` (E16-07 — true vector when both hold). |
| `export_batch` | `(doc_id, specs, dry_run=True, byte_budget?, out_dir?, name_prefix?)` | Run a **typed** list of export specs (`format` png/pdf/svg, optional `width_px`/`object_id`) in one bounded call. Per-call item cap + total-output byte budget (clamped to the per-doc artifact cap); `dry_run` defaults True (reports the plan + projected sizes, writes nothing). Composes the export engine; no new authority. |
| `export_set` | `(doc_ids, specs, dry_run=True, byte_budget?, out_dir?, name_prefix?)` | Batch-export a **set** in one call (E16-05): runs `export_batch`'s specs over every doc → per-doc `BatchResult` + aggregate (`total_items`, `total_bytes`) + a structured cross-doc `consistency` verdict. Composes the per-doc engine (not reimplemented); artifact-only. |

### Optimize — *medium, reversible*

| Tool | Signature | Description |
|---|---|---|
| `svg_web_optimize` | `(doc_id, precision=2, keep_ids?)` | Web-optimize the working copy: strip editor metadata / namespaced attrs / comments, drop unreferenced `defs`/ids/empty groups (referenced ids preserved — no dangling refs; ids in `keep_ids` are always retained, e.g. a deliberate a11y/human id), and reduce coordinate precision to `precision` decimals (0–8; root `viewBox` untouched). Returns structured deltas `{bytes_before, bytes_after, removed:{code:count}}` (codes cross-join with `quality_report.opportunities`). Direct-DOM (ADR-005); routed through the mutating pipeline → snapshot + Operation Record + before/after preview (reversible). |
| `optimize_set` | `(doc_ids, precision=2, keep_ids?)` | Web-optimize a **set** in one call (E16-05): runs `svg_web_optimize` over every doc → per-doc `WebOptimizeResult` + aggregate (`total_bytes_before`/`_after`/`_saved`, `changed_count`) + a structured cross-doc `consistency` verdict (computed on the pre-optimize state). Composes the per-doc engine; **one snapshot + Operation Record per CHANGED doc** (ADR-004). |

### Snapshots — *low*

| Tool | Signature | Description |
|---|---|---|
| `create_snapshot` | `(doc_id, label?)` | Snapshot the current working copy and index it. |
| `list_snapshots` | `(doc_id)` | List a document's snapshots in order, with metadata. |
| `restore_snapshot` | `(doc_id, snapshot_id)` | Revert the working copy to a chosen snapshot; returns `restored_sha256` + size so recovery is assertable without fs access. |
| `prune_snapshots` | `(doc_id)` | Apply the retention policy (keep-N / keep-days + hard caps), deleting superseded snapshots and orphaned Operation Records, **and** (E8-06) the document root's loop/live render frames by age + byte budget (never a frame referenced by a Live Operation Record). Never touches the working copy or original. Explicit maintenance sweep — also runs once at boot; never triggered implicitly by a mutating tool. |

### Style edits — *medium, reversible*

| Tool | Signature | Description |
|---|---|---|
| `set_fill` | `(doc_id, object_ids, color, opacity?)` | Set fill colour (and optional fill opacity). Colour is validated; CSS-injection punctuation rejected. |
| `set_stroke` | `(doc_id, object_ids, color?, width?, opacity?)` | Set stroke colour, width, and/or opacity. |
| `set_opacity` | `(doc_id, object_ids, opacity)` | Set element-level opacity (`[0, 1]`). |
| `replace_color` | `(doc_id, from_color, to_color, scope_ids?)` | Replace one colour with another across the document (or within `scope_ids` subtrees); matches inline styles and presentation attributes. |
| `apply_palette` | `(doc_id, mapping, scope_ids?)` | Apply many `from → to` colour replacements in a single reversible operation. |

### Text & object edits — *medium, reversible*

| Tool | Signature | Description |
|---|---|---|
| `replace_text` | `(doc_id, object_id, text)` | Replace the text content of a `<text>` / `<tspan>` / flow-text element. |
| `set_font` | `(doc_id, object_ids, family?, size?, weight?)` | Set font-family / font-size / font-weight (at least one required) on text objects. Returns `coverage_ok` + per-object `font_coverage` (`uncovered_chars`, `suggested_family`) so a non-covering family is caught at apply time (read from the font's own cmap, not fontconfig substitution). |
| `duplicate_object` | `(doc_id, object_id, new_id?)` | Duplicate an object/group in place, inserting the clone right after the original. |
| `tile` | `(doc_id, object_id, rows, cols, dx, dy)` | Replicate an object into an N×M grid (clone `(r,c)` offset by `(c·dx, r·dy)`) in one reversible call. Bounded count. |
| `rename_object` | `(doc_id, object_id, new_id?, label?)` | Change an object's `id` and/or `inkscape:label`. |
| `delete_object` | `(doc_id, object_ids, approval_token?)` | **High risk, reversible** (E16-08): remove objects by id from the DOM → `DeleteResult` (`EditResult` + `affected_ids`). Approval-gated (a real delete needs a non-empty `approval_token`; refused otherwise). Already-absent ids are skipped; no-match → `changed=false`, no snapshot. Pre-op snapshot + Operation Record per op; reversible via `restore_snapshot`. |

### Typed batch edit — *medium (max over members), reversible*

| Tool | Signature | Description |
|---|---|---|
| `apply_edits` | `(doc_id, edits, approval_token?)` | **Batch (E19-01):** apply an ordered list (≤ 64) of TYPED edits — a discriminated union over the existing DOM ops (`set_*` / `replace_*` / `apply_palette` / `replace_text` / `set_font` / `duplicate`/`rename`/`delete` / `move`/`scale`/`rotate`/`resize_canvas`/`normalize_viewbox`/`tile` / `create_*` / `group_objects`/`reparent`/`create_use` / `add_*_gradient`) — through the SAME edit kernel as ONE atomic operation. Validate-all first (one bad edit → document byte-identical), all-or-nothing rollback, **one snapshot + one Operation Record** (a single `restore_snapshot` reverts the whole batch). Effective risk = MAX over members; a `delete_object` member escalates the batch to HIGH and requires `approval_token`. Closes the round-trip tax vs a free-text `execute_code` without giving up typing/validation/reversibility (Penpot survey, E19). |
| `transform_objects` | `(doc_id, selector, operation, dry_run=True, max_matches=64, approval_token?)` | **Selector → op (E20-02):** declarative bulk edit without a code hatch — resolves a target SET via the EXISTING `find_objects` predicate engine (`tag`/`fill`/`stroke`/`text`/`id_prefix`/`bbox`, full CSS cascade) and applies ONE typed op (`set_fill`/`set_stroke`/`set_opacity`/`set_font`/`move_object`/`scale_object`/`rotate_object`/`delete_object`) to EVERY match, fanned out through the SAME atomic batch kernel as `apply_edits` (one snapshot + one Operation Record; all-or-nothing). Document-wide / create / identity-conflicting ops are rejected. `dry_run=True` (default) returns matched ids + the projected plan and writes nothing; `max_matches` (default 64) rejects an over-broad selector before any mutation. Effective risk = the op's class; a `delete_object` op is HIGH and requires `approval_token` (ADR-002/003/004). |

### Element creation — *medium, reversible*

Direct-DOM (ADR-005) shape primitives — one small typed tool per shape (no catch-all
`add_element(tag, attrs)` per ADR-002/003). Each inserts into an optional `parent_id` (must exist)
or the document default parent (first `inkscape:groupmode="layer"`, else the root), and returns a
`CreateResult` (the `EditResult` extended with `object_id` + an analytic `bbox`; `bbox` is `None` for
path/text whose geometry is not analytically cheap). Every value is strictly validated (finite
numbers, charset-safe ids, control-char-scrubbed text, command/charset-allowlisted path `d`).

| Tool | Signature | Description |
|---|---|---|
| `create_rect` | `(doc_id, x, y, width, height, parent_id?, object_id?, rx?, ry?, fill?, stroke?, stroke_width?)` | Insert a `<rect>` (size > 0; optional corner radii). Optional inline `fill`/`stroke`/`stroke_width` (E16-10b) paint it in the one call, validated like `set_fill`/`set_stroke`. |
| `create_circle` | `(doc_id, cx, cy, r, parent_id?, object_id?, fill?, stroke?, stroke_width?)` | Insert a `<circle>` (radius > 0); optional inline `fill`/`stroke`/`stroke_width`. |
| `create_ellipse` | `(doc_id, cx, cy, rx, ry, parent_id?, object_id?, fill?, stroke?, stroke_width?)` | Insert an `<ellipse>` (radii > 0); optional inline `fill`/`stroke`/`stroke_width`. |
| `create_line` | `(doc_id, x1, y1, x2, y2, parent_id?, object_id?, stroke?, stroke_width?)` | Insert a `<line>`; optional inline `stroke`/`stroke_width` (a line is unfilled — no `fill`). |
| `create_polygon` | `(doc_id, points, parent_id?, object_id?, fill?, stroke?, stroke_width?)` | Insert a closed `<polygon>` from `(x, y)` pairs; optional inline `fill`/`stroke`/`stroke_width`. |
| `create_polyline` | `(doc_id, points, parent_id?, object_id?, fill?, stroke?, stroke_width?)` | Insert an open `<polyline>` from `(x, y)` pairs; optional inline `fill`/`stroke`/`stroke_width`. |
| `create_path` | `(doc_id, d, parent_id?, object_id?, fill?, stroke?, stroke_width?)` | Insert a `<path>` with a strictly charset-validated, length-bounded `d`; geometry not parsed (`bbox=None`). Optional inline `fill`/`stroke`/`stroke_width`. |
| `create_text` | `(doc_id, x, y, text, parent_id?, object_id?, fill?, stroke?, stroke_width?)` | Insert a `<text>` holding `text` (stored as a text node; control chars rejected; `bbox=None`). Optional inline `fill`/`stroke`/`stroke_width` (font via `set_font`). |

### Defs, gradients & grouping — *medium, reversible*

Gradient defs land in the document `<defs>` (auto-created as the first child if absent); the returned
id is usable as a `url(#id)` paint. Grouping/structure tools reorganize existing objects.

| Tool | Signature | Description |
|---|---|---|
| `add_linear_gradient` | `(doc_id, stops, x1="0%", y1="0%", x2="100%", y2="0%", object_id?)` | Add a `<linearGradient>` to `<defs>`. `stops` = list of `{offset, color, opacity?}` (offset 0..1 or %, validated colour). Returns the gradient id; `bbox=None`. |
| `add_radial_gradient` | `(doc_id, stops, cx="50%", cy="50%", r="50%", fx?, fy?, object_id?)` | Add a `<radialGradient>` to `<defs>` (optional focal point). Returns the gradient id; `bbox=None`. |
| `create_group` | `(doc_id, parent_id?, object_id?)` | Insert an empty `<g>` to populate later. |
| `group_objects` | `(doc_id, object_ids, object_id?)` | Wrap existing objects (≥ 1, must exist) in a NEW `<g>` at the first target's position. |
| `reparent_object` | `(doc_id, object_id, new_parent_id)` | Move an object under a new parent (rejects a descendant/self parent; coordinate space may shift). |
| `create_use` | `(doc_id, href_id, parent_id?, object_id?, x?, y?, transform?)` | Insert a `<use href="#href_id">` to an existing same-document object (external/`javascript:`/`url(...)` hrefs rejected). Docstring notes the `<use x/y>` + `transform="scale"` translate-scaling trap. |

### Transforms — *medium, reversible*

| Tool | Signature | Description |
|---|---|---|
| `move_object` | `(doc_id, object_id, dx, dy)` | Translate by `(dx, dy)` in the parent coordinate space. |
| `scale_object` | `(doc_id, object_id, sx, sy?)` | Scale by `sx` (and `sy`, defaulting to `sx` for uniform). |
| `rotate_object` | `(doc_id, object_id, degrees, cx?, cy?)` | Rotate by `degrees` about `(cx, cy)` or the origin. |
| `resize_canvas` | `(doc_id, width, height, adjust_viewbox=False, bleed?, bleed_color="#ffffff")` | Set canvas `width` / `height` to validated CSS lengths; `adjust_viewbox=True` retargets the `viewBox` to track the new canvas. `bleed`>0 (E16-10d) ALSO grows the viewBox outward by `bleed` on every side and paints the new strip with `bleed_color` via one background `<rect>` behind content — a print-bleed resize in one call; mutually exclusive with `adjust_viewbox`; default off. |
| `normalize_viewbox` | `(doc_id)` | Normalize or repair the root `viewBox` (idempotent on a valid one). |
| `fit_to_content` | `(doc_id)` | Set the root `viewBox` to the document's content bounding box (computed via the Inkscape engine in the doc's intrinsic user-coordinate space). **Idempotent** — a second call on an already-fitted doc is a no-op (`changed: false`, no snapshot). Reversible op + snapshot on a real change. |

### Path geometry — *high (approval-gated), dry-run by default, reversible*

Destructive path operations that run through the Inkscape **engine** (`select-by-id;<action>`
arg-lists, never a shell string), not direct DOM (ADR-005). Every tool is HIGH risk: a real change
requires a non-empty `approval_token`; `dry_run` (typed param, **default `True`**) validates the
targets and reports which object ids + which Inkscape Action *would* run, writing nothing. Each
applied op is snapshotted + recorded + before/after-previewed (reversible). Object ids are
validated (argv-safe charset + must exist) before reaching the engine.

| Tool | Signature | Description |
|---|---|---|
| `simplify_path` | `(doc_id, object_ids, dry_run=True, approval_token?)` | Simplify path(s) (`path-simplify`), removing redundant nodes. |
| `boolean_union` | `(doc_id, object_ids, dry_run=True, approval_token?)` | Union ≥2 paths into one (`path-union`). Returns `result_id` = the surviving (bottom-most) id, chainable without a re-inspect. |
| `boolean_difference` | `(doc_id, object_ids, dry_run=True, approval_token?)` | Subtract the upper path(s) from the lowest (`path-difference`); needs ≥2 ids. Returns `result_id`. |
| `combine_paths` | `(doc_id, object_ids, dry_run=True, approval_token?)` | Combine ≥2 paths into one multi-subpath path (`path-combine`). Standardized to keep the bottom-most id (returned as `result_id`), matching the boolean ops. |
| `break_apart` | `(doc_id, object_ids, dry_run=True, approval_token?)` | Break a compound path into its subpaths (`path-break-apart`). |
| `stroke_to_path` | `(doc_id, object_ids, dry_run=True, approval_token?)` | Outline each stroke into a filled path (`object-stroke-to-path`). |
| `cleanup_paths` | `(doc_id, object_ids, dry_run=True, approval_token?)` | Remove redundant/degenerate path data (`path-simplify`). |

### Actions & extensions — *discovery low; chain execution high (approval-gated)*

A controlled way to use Inkscape **Actions** without an open-string passthrough (ADR-003). Discovery
is probe-driven (reuses `inkscape --action-list`); an execution surface is built from a **typed,
ordered chain** of `ActionStep`s — never a raw string. Every step is validated against the
**server-side allowlist** (`INKSCAPE_MCP_ACTION_ALLOWLIST`, env-additive onto built-in defaults —
never client-supplied) **and** a **versioned Action capability map** (persisted at
`<root>/.inkscape-mcp/action-maps/<version>.json`, keyed by detected Inkscape version) so an Action
absent on the host is refused cleanly. Chain execution runs through the Inkscape engine (arg-lists,
`shell=False`) over the working copy and is snapshotted + recorded + before/after-previewed. The
single-Action **raw escape hatch** (`run_raw_action`, ADR-003) reuses the same gates behind an
opt-in, OFF-by-default advanced-mode switch (`INKSCAPE_MCP_RAW_ACTION_ENABLED`).

| Tool | Signature | Description |
|---|---|---|
| `list_actions` | `()` | Discover the host's actual Action surface + the allowlisted/available subsets; persists the version-keyed capability map. |
| `discover_extensions` | `()` | List the server-side allowlisted extension set + probe notes (diagnostic; nothing executes; empty by default). |
| `validate_action_chain` | `(steps)` | **Dry-run**: validate a typed `ActionStep` chain against the allowlist + capability map + charset; return the resolved `--actions` argument + argv preview with no invoke/write. Invalid chains refused with a machine-readable error code. |
| `run_action_chain` | `(doc_id, steps, approval_token?)` | **High risk**: run a validated chain over the working copy via the mutating pipeline (snapshot + Operation Record + before/after preview, reversible). Requires a non-empty `approval_token`. |
| `run_raw_action` | `(doc_id, action, args?, dry_run=True, approval_token?)` | **High risk, advanced mode (OFF by default).** The ADR-003 escape hatch: run ONE allowlisted Action (typed `action` + `args`, never a raw string). Refused with `raw_action_disabled` unless `INKSCAPE_MCP_RAW_ACTION_ENABLED` is set. Reuses the same allowlist + capability-map + charset validation; defaults to `dry_run=True` (resolved argv, no mutation); a real run requires a non-empty `approval_token` and routes through the mutating pipeline (snapshot + Operation Record + before/after preview, reversible). |

### Save — *medium / high (approval-gated)*

| Tool | Signature | Description |
|---|---|---|
| `save_document_as` | `(doc_id, dest_path, overwrite=False, approval_token?)` | Save the working copy to a **new** file (validated before & after). New file = medium risk. Overwriting an existing file requires `overwrite=True` **and** a non-empty `approval_token` and is recorded as high-risk. Originals and managed sources are never touched. |

### Live mode (E3 read / E4 write / E8 view loop) — *on by default (operator-chosen)*

Control of a **running** Inkscape, cross-platform via a transport abstraction (extension-socket
bridge on any OS; DBus `org.gtk.Actions` an optional Linux fast-path). Gated by
`INKSCAPE_MCP_LIVE_ENABLED` (default on; set falsy to opt out); absent/unsupported transports are
reported cleanly, never as errors. **No-freeze (E3-07):** the socket bridge is a *modal* `inkex`
effect extension (freezes the GUI for the whole session); the Linux DBus path runs in Inkscape's own
main loop and does **not** freeze the GUI — `live_connect(prefer="no_freeze")` selects it on Linux for
viewport, style/transform writes, and a structured export-to-file read (live SVG/PNG/active-doc).
Selection-id reads stay on the (modal) socket path; Windows/macOS live stays modal (best-effort).
The command schema is a fixed enum (wire **protocol v5**) — no arbitrary code or raw Action
passthrough (ADR-003). E4 adds **semantic write**: the three mutating tools are HIGH risk and require
an explicit `approval_token`, each producing a Live Operation Record with before/after canvas
renders; live never mutates unapproved. E8 adds the **view loop**: view-only viewport/region tools and
**structured perception** — `live_get_scene` pairs each rendered frame with a machine-readable
`LiveScene` (active-doc ref, selection ids + bboxes, viewport, canvas size, visible-object summary
reusing the headless `ObjectInfo` shape) so the agent reasons over structure, not pixels (ADR-006) —
plus **change detection**: `live_wait_for_change` polls a CHEAP server-hashed state token (revision +
selection + viewport — never the full doc or a PNG) on a bounded, cancelable wait so the loop renders
ONLY on change (including the user's own GUI edits), never busy-rendering. And a **focused visual
diff**: `live_diff_view` reuses a mutation's captured before/after frames, pixel-diffs them to a
changed-region bbox, and emits ONE annotated overlay (changed bbox + selection outline) linked back to
the Live Operation Record — a targeted diff, not two raw whole-window screenshots.
View/perception/change/diff tools are LOW risk — no document mutation, no Operation Record, no approval.
Finally, the **loop orchestrator** `live_session_step` frames ONE perceive→decide→act→observe
iteration: it captures the `LiveScene` + frame (perceive), the agent picks ONE typed semantic act from
a FIXED set (`apply`/`insert_svg`/`set_text` — each 1:1 with an E4 write engine, no raw-Action/code),
routes it through `run_live_mutation` (HIGH + `approval_token` + Live Operation Record — the SAME E4
write path, zero new authority per ADR-006), then captures an after-scene + a focused `live_diff_view`
(observe). With no act it is perceive-only (no record). It is **bounded + cancelable by construction** —
a single step is one iteration; the agent drives the loop by re-calling it (there is no server-side
autonomous runner). The `live_canvas_assist` **Prompt** is the §4.1 entry point that orients the agent
on this loop.

| Tool | Signature | Risk | Description |
|---|---|---|---|
| `check_live_support` | `()` | low | Report every live transport probed on this host (not assumed by OS), the best read-capable one, and whether the helper is installed. |
| `live_connect` | `(prefer="read")` | medium | Connect over the best-ranked transport; records the chosen transport + active document. `prefer="read"` (default) = full-read socket (modal); `prefer="no_freeze"` = Linux DBus no-freeze action path (no selection-id reads). Requires the master gate. |
| `live_status` | `()` | low | Current session state: enabled, connected, active transport, available transports. Never raises. |
| `live_disconnect` | `()` | low | Tear down the live session (the X1 disable switch). Idempotent. |
| `live_install_helper` | `()` | restricted | Install the shipped extension-socket helper into the Inkscape user extensions dir. Gated by the master switch. |
| `live_arm_socket` | `()` | restricted | AUTO-ARM the socket helper (E16-10f): install it (idempotent) then launch a headful Inkscape with the helper effect auto-invoked via `--actions` (fixed arg-list, no shell) so the loopback socket binds with NO Extensions-menu click — then `live_connect` gets the full perceive/compose surface, not just DBus's reduced set. Socket bridge stays the cross-platform primary. GUI-ONLY: on a headless host (no `DISPLAY`/`WAYLAND_DISPLAY`) it fails with a clear message rather than spawning a doomed process. Gated by the master switch. |
| `live_get_active_document` | `()` | low | Identity of the document open in the live instance. |
| `live_get_selection` | `()` | low | Current selection as object ids. |
| `live_inspect_selection` | `()` | low | Per-object detail for the selection (reuses the headless `ObjectInfo` shape). |
| `live_render_view` | `(region_x?, region_y?, region_width?, region_height?, scale?, fast?)` | low | Rasterize the live canvas to a PNG under the live artifacts dir. Optional region/bbox (all four together) + `scale` render a targeted, downscalable frame; `fast=True` applies the documented half-res loop preview (explicit `scale` wins). Served from the per-session render cache keyed `(doc_revision, viewport, scale)` so a hit skips re-render and a stale frame can never follow a doc change (E8-06). Numerics bounded server-side; transport-rendered, never an OS screenshot (ADR-006). View-only, no record. |
| `live_set_viewport` | `(mode, zoom?, center_x?, center_y?, dx?, dy?)` | low | Control the live canvas viewport: `mode` ∈ `zoom`/`pan`/`fit_selection`/`fit_page` (fixed semantic verbs). Numerics bounded server-side. View-only — no document mutation, no Operation Record, no approval. |
| `live_get_scene` | `(region_x?, region_y?, region_width?, region_height?, scale?, fast?)` | low | Capture one live frame: the rendered PNG **plus** a structured `LiveScene` (active-doc ref, selection ids + bboxes, viewport, canvas size, visible-object summary reusing `ObjectInfo`). Region/scale/`fast` work as `live_render_view` (cached frame, E8-06). Scene pulled over the fixed `get_scene` command (protocol v4). Read-only perception — no mutation, no Operation Record (E8-02). |
| `live_wait_for_change` | `(timeout_s=5.0, poll_interval_s=0.5)` | low | Block until the live state changes or the bounded timeout elapses. Polls a CHEAP server-hashed state token (revision + selection + viewport — never the full doc or a PNG; `get_state_token`, protocol v5) and classifies the delta as `selection_changed` / `document_changed` / `viewport_changed`. Bounded + cancelable (`timeout_s` capped at 60s, sleeps between polls — no busy-loop); detects the user's own GUI edits. Read-only — no mutation, no Operation Record (E8-03). |
| `live_sync_to_workspace` | `(dest_path)` | medium | Save the live document as a **new** tracked workspace document (atomic write, never overwrites; Operation Record + snapshot). |
| `live_apply_to_selection` | `(approval_token, fill?, stroke?, stroke_width?, opacity?, dx?, dy?, scale?, rotate?)` | **high** | Apply a validated style and/or simple transform to the live selection (reuses E2 semantics). Approval-gated; Live Operation Record + before/after render. |
| `live_insert_svg` | `(svg_fragment, approval_token)` | **high** | Insert a safe-parsed SVG fragment into the running document. Approval-gated; recorded + rendered. |
| `live_set_selected_text` | `(text, approval_token)` | **high** | Replace the selected text object's content (length/control-char guarded). Approval-gated; recorded + rendered. |
| `live_export_selection` | `()` | low | Export just the current live selection to a PNG under the live artifacts dir (read-only feedback, no record). |
| `live_diff_view` | `(operation_id)` | low | Produce a FOCUSED, annotated before/after visual diff of a live op — not two raw window screenshots. REUSES the op's E4-02 `preview_before`/`preview_after` frames (resolved via the operation id, sandbox-validated), pixel-diffs them (`ImageChops.difference(...).getbbox()`) to a changed-region bbox, and emits ONE overlay (changed bbox + selection outline from the `LiveScene`). Server-minted PNG under the live artifacts dir; returns the workspace-relative path + the pixel changed bbox; linked back to the Live Operation Record (`diff_artifacts`). Artifact-only — no mutation, no record, no approval (E8-04). |
| `live_session_step` | `(action?, approval_token?, fill?, stroke?, stroke_width?, opacity?, dx?, dy?, scale?, rotate?, svg_fragment?, text?)` | low when perceive-only / **high** when it acts | Run ONE perceive→decide→act→observe loop iteration (E8-05). PERCEIVE = `LiveScene` + frame (always, read-only). With no `action` it is perceive-only (no record). With `action` ∈ the FIXED set `apply`/`insert_svg`/`set_text` (each 1:1 with an E4 write engine — no raw-Action/code), the ACT runs through `run_live_mutation` (HIGH + `approval_token` + Live Operation Record + before/after frames — the SAME E4 write path, zero new authority), then OBSERVE captures an after-scene + a `live_diff_view` linked to the record. Bounded + cancelable by construction (single-step primitive; the agent drives the loop). |

The helper extension can also be installed without the server via the dynamic installers in
[`scripts/`](scripts/): `install-live-helper.sh` (Linux / macOS / Windows under Git Bash or WSL)
and `install-live-helper.ps1` (native Windows PowerShell). Both resolve the Inkscape user
extensions dir at runtime (`inkscape --user-data-directory`, with `INKSCAPE_PROFILE_DIR` and an
OS-aware fallback) and do not require the master gate.

## Resource reference

Read-only MCP resources addressable by URI. Document resources are templated on `doc_id`.

| URI | Description |
|---|---|
| `inkscape://runtime/capabilities` | Cached runtime capability matrix, including the authoritative MCP tool surface (`tool_count` + `tools:[{name, purpose, risk}]`, from the live registry — E16-01). |
| `inkscape://runtime/intents` | Curated goal→tool intent map (`[{goal_pattern, tools, how_to, group}]`) — the same map `how_do_i` / `list_capabilities` use, without the full capabilities payload. |
| `inkscape://documents` | Index of open documents and their concrete per-doc resource URIs (discoverable via `ListMcpResourcesTool`). |
| `inkscape://prompts` | Index of registered MCP prompts (name + one-line purpose + arguments, from the live `mcp.list_prompts()` registry — E16-10e), so the prompt library is discoverable via `ListMcpResourcesTool` (prompts are otherwise a separate MCP capability the resource surface can't see); fetch a prompt's text via the MCP prompts API. |
| `inkscape://document/{doc_id}/summary` | Top-level document summary. |
| `inkscape://document/{doc_id}/tree` | Element tree. |
| `inkscape://document/{doc_id}/layers` | Layer list. |
| `inkscape://document/{doc_id}/objects` | Object inventory. |
| `inkscape://document/{doc_id}/styles` | Style usage. |
| `inkscape://document/{doc_id}/fonts` | Fonts referenced. |
| `inkscape://document/{doc_id}/assets` | External assets / references. |
| `inkscape://live/session` | Live-session state (enabled / connected / transport). Clean when no session. |
| `inkscape://live/selection` | Current live selection (object ids). Empty when no session. |
| `inkscape://live/view` | Current live frame's structured metadata: the `LiveScene` (selection ids + bboxes, viewport, canvas size, visible-object summary), without PNG bytes. Empty when no session (E8-02). |
| `inkscape://live/events` | Latest live change state: the current cheap state token + classified deltas (`selection_changed` / `document_changed` / `viewport_changed`). Read-only; empty `LiveChange` when no session (E8-03). |
| `inkscape://live/operations` | Recent Live Operation Records (E4): what each mutation changed, approval, before/after renders. Paths are workspace-relative or opaque (`<external>`) — never a host path — and cleared at each session boundary. Empty when none. |

## Prompt reference

MCP Prompts orient the agent on how to use the tool surface safely; they grant no capability of
their own (architecture §4.1).

| Prompt | Args | Description |
|---|---|---|
| `live_canvas_assist` | `(goal)` | Entry point for the live-view co-pilot loop (E8-05). Orients the agent to drive a running Inkscape toward `goal` via `live_session_step`, one bounded perceive→decide→act→observe iteration at a time: perceive the `LiveScene`, pick ONE semantic act from the fixed set (`apply`/`insert_svg`/`set_text`), act through the approval-gated `run_live_mutation` path, observe the focused diff, and react to user edits with `live_wait_for_change`. Emphasizes that acts are semantic-only + approval-gated and the loop is bounded/cancelable — the loop adds zero new authority. |
| `prepare_web_export` | `()` | Orients the agent on producing web-ready assets (optionally optimize + quality-check first, then `export_web_profile` / `export_batch`). Guidance only (E5-07). |
| `prepare_icon_set` | `()` | Orients the agent on producing a multi-size square PNG icon set via `create_icon_set`. Guidance only (E5-07). |
| `prepare_print_export` | `()` | Orients the agent on producing a print-ready vector PDF via `export_print_profile` (validate fonts/assets first). Guidance only (E5-07). |
| `theme_recoloring` | `()` | Orients the agent on recoloring to a brand/theme palette via `replace_color` / `apply_palette` (validated, reversible). Guidance only (E5-07). |
| `compose_artwork` | `(goal)` | On-ramps the E14 generative loop toward `goal` (E15-04): `create_document` → `create_*` shapes / `add_*_gradient` + `set_fill` (incl. `url(#id)` paint) / `create_group` (+ `find_objects` to address ids) → `render_preview` (inline raster) → `validate_document` → `export_document`, with `restore_snapshot` reversibility. Guidance only. |
| `restyle_artwork` | `(goal)` | On-ramps the OBJECT-TARGETED restyle loop toward `goal` (E15-04): `find_objects` to address ids → per-object `set_fill` / `set_stroke` / `set_opacity` (or `set_font` / `replace_text`) → `render_preview` → `export_document`; companion to the document-wide `theme_recoloring`. Guidance only. |

## Try asking your agent to…

Natural-language asks and the tool(s) each exercises. This catalog is **aligned with the same
curated goal→tool map** that powers the `how_do_i` tool and the `intents` section of
`list_capabilities` ([`src/inkscape_mcp/intents.py`](src/inkscape_mcp/intents.py)) — so the doc, the
discovery tool, and the runtime matrix never diverge. Don't know which tool fits? Just ask
`how_do_i("…")` with the goal in words and it returns the same mapping. For the full create→render→
export workflow, snapshots/reversibility, and the risk/approval model see the
[agent-usage guide](docs/agent-usage-guide.md).

**Generate / draw**

- "Draw a rectangle / circle / line on a new canvas." → `create_document`, `create_rect`,
  `create_circle`, `create_line`
- "Add a text label." → `create_text`
- "Add a gradient fill (linear or radial)." → `add_linear_gradient`, `add_radial_gradient`, `set_fill`
- "Group these objects together." → `group_objects`, `create_group`

**Edit**

- "Make this shape blue / change its fill." → `set_fill`
- "Change the stroke / outline and opacity." → `set_stroke`, `set_opacity`
- "Swap one colour for another across the whole document." → `replace_color`, `apply_palette`
- "Move / scale / rotate an object." → `move_object`, `scale_object`, `rotate_object`
- "Delete these objects by id." (HIGH-risk, approval-gated, reversible) → `delete_object`
- "Resize the canvas or fit it to the content." → `resize_canvas`, `fit_to_content`, `normalize_viewbox`
- "Simplify / clean up these paths." (HIGH-risk, dry-run first) → `simplify_path`, `cleanup_paths`
- "Union / subtract these shapes." (HIGH-risk) → `boolean_union`, `boolean_difference`, `combine_paths`

**Inspect**

- "Open this SVG and tell me what's in it." → `open_document`, `inspect_document`
- "Find the red shapes / all text / objects by id." → `find_objects`
- "Is this document valid? Give me a quality report." → `validate_document`, `quality_report`
- "Audit a whole icon system for consistency (viewBox/stroke/id naming)." → `quality_report_set`
- "What's the byte size / sha256 of this file (or this set)?" → `stat_artifact`, `stat_artifacts`
- "What can this server do?" → `list_capabilities`, `how_do_i`

**Export**

- "Export a 512 px PNG (or a preview)." → `export_document`, `render_preview`
- "Export just this one object." → `export_object`
- "Export a whole icon set / many sizes at once." → `create_icon_set`, `export_batch`
- "Lay out a 12-icon system as a contact/spec sheet in one call." → `compose_grid`
- "Export / optimize a whole set of documents at once." → `export_set`, `optimize_set`
- "Make the SVG smaller for the web." → `svg_web_optimize`
- "Export web-ready / print-ready assets." → `export_web_profile`, `export_print_profile`
- "Save it to a new file." → `save_document_as`

**Live**

- "Snapshot / undo / restore the document state." → `create_snapshot`, `list_snapshots`, `restore_snapshot`
- "Connect to my running Inkscape and work on the open canvas." → `live_connect`, `live_get_scene`,
  `live_apply_to_selection`

## Safety model

The server is built to be safe to hand to an autonomous agent:

- **Workspace sandbox.** Every path is normalized and resolved; symlinks are guarded; access
  outside the configured workspace root(s) is rejected before any I/O.
- **Originals untouched.** Documents are opened as working copies. No tool overwrites the source;
  `save_document_as` writes elsewhere, and overwriting an existing file needs explicit approval.
- **Reversibility.** Mutating tools snapshot first and emit an Operation Record; `restore_snapshot`
  rolls the working copy back to any indexed snapshot. An explicit retention sweep (boot-time +
  `prune_snapshots`) bounds snapshot growth without ever pruning the baseline or the live head.
- **Risk policy.** `low`/`medium` are permitted; `high` requires a per-operation `approval_token`
  (minted out of band, bound to one operation — never an ambient flag a model can set); `restricted`
  (code / network / fs-escape) never ships in the MVP.
- **Subprocess hygiene.** The Inkscape CLI is always invoked with **argument lists, never shell
  strings**. Object ids and formats are charset-validated before reaching the binary.
- **Resource limits.** Input/output/export size caps, per-process timeouts, and a concurrency cap.
- **Safe XML.** Parsing disables entity expansion and external-entity resolution (no XXE / billion
  laughs). No network access. No arbitrary extension execution.
- **Stable errors.** Tools raise `ToolError`s with host-path-free public messages; full detail goes
  to stderr logs only (stdout is the MCP channel).

## Project layout

```
src/inkscape_mcp/
  server.py     # FastMCP app + STDIO entry point (register_tools wires every module)
  config.py     # process Settings + operator-tunable limits (env-driven)
  registry.py   # doc_id <-> path registry; opens working copies, never mutates originals
  operations.py # Operation Record model + persistence (ADR-004)
  snapshots.py  # snapshot engine + reversible restore
  retention.py  # snapshot + live-frame retention/cleanup (keep-N/keep-days/hard caps + live-frame
                #   age/byte caps; explicit boot sweep + prune tool — never implicit)
  validate.py   # read-only validation engine
  quality.py    # E5-05 read-only quality report (wraps validate + inspect + E5-04 optimizer counts)
  logging_setup.py # stderr-only structured logging (stdout reserved for MCP STDIO)
  document/     # direct-DOM inspection engine (summary/tree/layers/styles/fonts/assets)
  edit/         # safe-edit engines: dom.py (lxml primitives + shared SAFE_ID_RE), pipeline.py
                #   (snapshot + before/after preview + Operation Record wrapper; risk-classed),
                #   style.py, text_object.py, transform.py, create.py (E14-01/E14-04 element-creation
                #   + defs/gradients + grouping engines), optimize.py (E5-04 web-optimize: strip editor
                #   cruft + drop dead structure + reduce coord precision; reversible), paths.py
                #   (E6 HIGH-risk path geometry via the Inkscape engine — arg-list Actions →
                #   safe-parse → DOM-replace)
  actions/      # E6-02 controlled Action surface: capability_map.py (version-keyed Action map +
                #   discovery, persisted under action-maps/), chains.py (typed ActionStep chains →
                #   allowlist+map validation → arg-list --actions argv → engine; no raw passthrough;
                #   reused by the E6-03 run_raw_action escape hatch)
  render/       # Inkscape CLI render/export engine (cli.py) + export profiles (profiles.py) +
                #   E5-06 bounded typed batch export (batch.py: item cap + byte budget + dry-run) +
                #   E16-07 in-process content-truth verifier (verify.py: PDF is_vector/fonts_outlined,
                #   raster opaque_px/all_blank — no pdffonts/Pillow subprocess)
  engine/       # E12 / ADR-007 opt-in warm `inkscape --shell` engine: process.py (one supervised
                #   worker — read-until-prompt framing, per-command timeout+kill, crash/idle reap),
                #   manager.py (per-working-copy worker pool, serialized, LRU, freshness reopen),
                #   ops.py (shell export/action composition). Gated by INKSCAPE_MCP_ENGINE_MODE=shell
                #   with an automatic per-call CLI fallback; render/paths/chains route through it.
  live/         # E3 live read + E4 live write + E8 live view: transport ABC + capability-aware
                #   backend selection, extension-socket + DBus backends, protocol.py wire schema
                #   (v3: read + semantic write + view-only commands, region/scale render),
                #   session manager, render/sync, edit.py (write + view engine reusing E2 semantics
                #   + bounded view validators), records.py (Live Operation Records + approval gate),
                #   scene.py (E8-02 LiveScene), diff.py (E8-04 focused visual diff), loop.py (E8-05
                #   perceive→decide→act→observe orchestrator — composes the above, zero new authority),
                #   cache.py (E8-06 bounded LRU render cache keyed (doc_revision, viewport, scale) +
                #   coalescing budget; freshness via the revision key), helper_extension/ (runs inside
                #   Inkscape). Gate on by default.
  prompts/      # MCP Prompts (architecture §4.1): live.py (live_canvas_assist — live-view loop entry
                #   point), library.py (E5-07 export/recolor orientation prompts), authoring.py
                #   (E15-04 compose_artwork / restyle_artwork — E14 generative on-ramp prompts)
  resources/    # MCP resource templates (runtime caps, document/{id}/..., live/{session,selection,operations})
  runtime/      # Inkscape capability probe
  tools/        # typed @mcp.tool modules: system, document, validate, quality (E5-05), export,
                #   profiles, export_batch (E5-06), optimize (E5-04), snapshots, style,
                #   text_object, create (E14-01/E14-04 element-creation + defs/gradients + grouping),
                #   transform, paths (E6 path geometry), actions (E6-02 discovery +
                #   Action chains + E6-03 run_raw_action), save, live (read + write)
  workspace/    # sandbox/path safety, limits, risk policy, safe XML parse, subprocess wrapper
scripts/        # install-live-helper.sh (Linux/macOS/Git-Bash) + .ps1 (Windows): copy the live
                #   helper extension into Inkscape's user extensions dir, resolved dynamically;
                #   gen_llms_txt.py (E15-01): regenerate llms.txt / llms-full.txt from the live registry
llms.txt        # GENERATED LLM index (one line + risk class per tool); do not hand-edit
llms-full.txt   # GENERATED full manifest (full descriptions + key params + prompts + resources)
evals/          # E15-06 deterministic tool-usability harness: tool_selection_scenarios.json +
                #   run_eval.py (drives how_do_i/intents; reports tool-selection accuracy). ruff-checked.
                #   run_live_eval.py (E15-06a): OPTIONAL report-only live-agent runner (same dataset/scorer).
examples/       # ready-to-copy MCP host configs (claude_desktop_config.json, mcp.json)
tests/          # pytest; Inkscape-dependent tests marked @pytest.mark.inkscape
```

Install / config / compatibility / troubleshooting docs: [`docs/install/`](docs/install/).
Driving the server from an agent (create→render→export loop, snapshots, risk/approval gate, tool
selection): [`docs/agent-usage-guide.md`](docs/agent-usage-guide.md).

## Development

- **Stack:** Python ≥ 3.12 · `uv` · FastMCP 3.x · `lxml` · Pillow (live visual diff) · Inkscape CLI (per-call, plus an opt-in warm `inkscape --shell` engine — E12/ADR-007) · STDIO transport.
- **Conventions:** small typed tools (no portmanteau / `run_action(string)`), risk-classed,
  mutating ops emit Operation Records and never overwrite originals, subprocess via arg-lists.
  See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full contributor workflow + conventions.
- **Lint / format:** `ruff` (selects `E,F,I,B,UP,S,RUF`; `S603`/`S607` are intentionally ignored
  because the Inkscape CLI adapter needs subprocess — safety enforced by arg-lists + review).
- **Types:** `mypy --strict` over `src`.
- **Tests:** `pytest`; mark Inkscape-binary tests with `@pytest.mark.inkscape`.

```bash
uv run pytest                       # full suite
uv run pytest -m "not inkscape"     # skip tests needing the Inkscape binary
uv run ruff check --fix . && uv run ruff format .
uv run mypy src
```


## License

MIT © Johannes Sood
