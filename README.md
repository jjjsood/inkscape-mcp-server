# inkscape-mcp

> A Model Context Protocol (MCP) server that makes Inkscape / SVG documents **agent-ready** —
> inspect, edit safely, validate, render, and export vector graphics from any MCP client.

[![Python](https://img.shields.io/badge/python-%E2%89%A53.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastMCP](https://img.shields.io/badge/MCP-FastMCP%203.x-5A45FF)](https://github.com/jlowin/fastmcp)
[![Transport](https://img.shields.io/badge/transport-STDIO-444)](#connecting-an-mcp-client)
[![License](https://img.shields.io/badge/license-MIT-green)](#license)

`inkscape-mcp` exposes a small, result-oriented tool surface over your SVG documents: **ten tools**,
where the typed operations are *ops* inside one atomic `edit` call. An LLM agent can open a drawing,
read its structure, recolour and re-letter objects, transform geometry, render previews, and export
production assets — all **headless-first** and **reversible by construction**.
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
- [License](#license)

---

## Why

LLM agents are good at reasoning about *what* should change in a drawing ("make the logo blue,
bump the heading to 24 px, export a 512 px icon") but bad at safely poking at raw XML or driving a
GUI. A naive "run this Inkscape command" tool is dangerous: it can overwrite originals, shell out
unsafely, or silently corrupt a file with no way back.

`inkscape-mcp` solves that with a **bounded, typed API**: every capability is a typed op or tool
action with explicit parameters and a declared risk class — no free-text escape hatch. Simple structural edits go through a direct
`lxml` DOM layer; rendering, export, and complex geometry go through the Inkscape CLI. Originals
are never mutated, every change is snapshot-backed and reversible, and subprocess calls use
argument lists — never shell strings.

## Highlights

- **Ten result-oriented tools.** `open` → `edit` (many typed ops, one atomic call, `$n` references,
  selectors, inline render) → `export`, plus `inspect`, `render`, `history`, `help` and — when
  enabled — `live`, `live_edit` and `actions`. A logo is two calls instead of ten, and `tools/list`
  is ≈15 KB instead of ≈325 KB.
- **Headless-first.** No GUI required; the Inkscape binary is used only for render / export /
  geometry, and the server probes the runtime instead of assuming a version.
- **Reversible by construction.** Every mutating call = one pre-mutation snapshot + one Operation
  Record. `history(action='restore')` rolls back.
- **Originals are sacred.** Documents open into tracked working copies. Nothing writes over the
  source file; saving goes to a *new* path, and overwrites are gated behind explicit approval.
- **Risk-classed.** Every call is `low` / `medium` / `high` / `restricted` (computed from its ops);
  the policy layer enforces it (high-risk needs a per-operation approval token; restricted never
  ships in the MVP).
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
   FastMCP app  ──►  ten @mcp.tool functions + typed ops  (risk-classed, validated args)
        │
        ├─ direct-DOM engine (lxml)        → structure read + safe edits
        ├─ Inkscape CLI adapter (arg-list) → render / export / geometry
        ├─ snapshot engine                 → pre-mutation copy + reversible restore
        ├─ Operation Records               → audit trail of every mutation
        └─ workspace sandbox               → path/size/timeout/XML safety
```

1. **`open`** copies your SVG (or creates a blank canvas) into a tracked workspace document and
   hands back an opaque `doc_id`. The original file is never opened for writing again.
2. **`inspect` / resources** read that working copy — tree, layers, styles, fonts, assets.
3. **`edit`** mutates the working copy through the pipeline: validate every op → take a snapshot →
   apply the ops → write an Operation Record. All medium-risk and reversible; HIGH ops need an
   approval token. `render='inline'` returns a picture of the result.
4. **`render` / `export`** shell out to the Inkscape CLI (argument lists only) and drop artifacts
   into the workspace artifacts / exports directories as workspace-relative paths.
5. **`export` with a `save_as` target** writes the working copy to a *new* file (validated before
   and after). Overwriting an existing file is a separate, approval-gated high-risk path.

## Requirements

- **Python ≥ 3.12** and [`uv`](https://docs.astral.sh/uv/).
- Runtime deps (installed by `uv sync`): **FastMCP 3.x**, **lxml**, and **Pillow** (the focused
  live before/after visual diff, `live(action='diff')`, uses Pillow to pixel-diff frames and draw the
  annotation overlay).
- **Inkscape on `PATH`** for the render / export / geometry tools (developed and tested against
  Inkscape **1.4.x**). Read / edit / validate tools work without it.
  - Probe your install with `inkscape --version` / `inkscape --action-list`, or run the
    `help(topic='diagnose')` tool.
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
uvx --from "git+https://github.com/jjjsood/inkscape-mcp.git" inkscape-mcp

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

**CI.** [`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs ruff + ruff-format
+ mypy + pytest on Linux/macOS/Windows (headless + the cross-platform live-transport suite), the
full suite incl. real-Inkscape tests on Linux, and a packaged `pipx`-install STDIO boot smoke on all
three OSes, plus a full-surface MCP smoke (`ci_surface_smoke.py`) that asserts the registered
primitive counts (**10 tools / 7 prompts / 17 resources**) and reads every resource over an in-memory
client. CI helper scripts live in [`scripts/`](scripts/) (`ci_diagnostics.py`, `ci_boot_smoke.py`,
`ci_surface_smoke.py`).

**Evals.** [`evals/`](evals/) holds a deterministic, CI-runnable tool-usability harness that
makes "agent-friendly" measurable without a live LLM: `tool_selection_scenarios.json` is a labelled
set of natural-language asks (mirroring the intent catalog below) whose expected answers are
`tool:op` targets (`edit:create_rect`, `open:canvas`, `export:profile`, …), and `run_eval.py` drives
the server's own discovery layer (`help(goal=…)` / [`intents.py`](src/inkscape_mcp/intents.py)) to
report per-group + overall tool-selection accuracy and out-of-scope flagging (`uv run python
evals/run_eval.py`, `--json` for the report dict). `tests/test_eval_harness.py` gates it against
regressions and checks every expected `tool:op` really exists; the scenario schema is
runner-agnostic. `run_call_count.py` measures calls and response bytes for three reference
scenarios (logo + export: 2 calls; recolor + web export: 3; live recolor: 2). An OPTIONAL,
report-only live-agent runner (`run_live_eval.py`) reuses the SAME dataset + scorer to capture real
tool-call traces + turn count via a pluggable `AgentDriver` (deterministic `ReplayDriver` by default;
the real MCP+LLM path is off unless `--driver real` / `INKSCAPE_MCP_EVAL_DRIVER=real`) and never
gates CI.

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
| `INKSCAPE_MCP_LIVE_CACHE_MAX_ENTRIES` | `64` | Max frames in the per-session live render cache (LRU). Floored. |
| `INKSCAPE_MCP_LIVE_CACHE_MAX_BYTES` | `268435456` (256 MiB) | Total byte budget for the live render cache (LRU eviction). Floored. |
| `INKSCAPE_MCP_LIVE_COALESCE_BUDGET_MS` | `200` | Frame-coalescing latency budget: a repeated identical-key render within this window returns the just-cached frame instead of re-rendering. `0` disables. |
| `INKSCAPE_MCP_LIVE_FRAME_KEEP_DAYS` | `7` | Age retention for loop/live render frames, pruned by the **explicit** retention sweep (boot + `history(action='prune')`), never implicitly by a mutating tool. |
| `INKSCAPE_MCP_LIVE_FRAME_MAX_BYTES` | `536870912` (512 MiB) | Total byte budget for loop/live render frames (newest kept), pruned by the explicit sweep. |
| `INKSCAPE_MCP_LIVE_ENABLED` | `true` | Master gate for live mode. **On by default** (operator-chosen); set a falsy value (`0`/`false`/`no`/`off`) to opt out, in which case `live` / `live_edit` are hidden. |
| `INKSCAPE_MCP_LIVE_RENDEZVOUS` | *(none)* | Optional explicit path to the live helper's rendezvous file (otherwise discovered under the Inkscape user data dir / temp dir). |
| `INKSCAPE_MCP_ACTION_ALLOWLIST` | *(built-in defaults)* | OS-path-separated list of Inkscape Action ids **added** to the built-in allowlist. Server-side, never client-supplied; cannot remove a default or open arbitrary passthrough. Each Action must also exist in the version-keyed capability map to run. |
| `INKSCAPE_MCP_EXTENSION_ALLOWLIST` | *(empty)* | OS-path-separated list of Inkscape extension ids added to the (empty) execution allowlist. Discovery is read-only and unaffected. |
| `INKSCAPE_MCP_RAW_ACTION_ENABLED` | `false` | Advanced-mode gate for the `actions` tool and the HIGH-risk path ops (ADR-003). **Off by default**; set a truthy value (`1`/`true`/`yes`/`on`) to opt in. Enabling it does **not** widen the allowlist — every Action still has to be allowlisted, present in the capability map, charset-safe, and (for a real run) HIGH + approval-token-gated. |
| `INKSCAPE_MCP_EDIT_PREVIEWS` | `none` | Per-edit preview frames on the Operation Record: `none` (default, zero Inkscape renders per edit), `after`, or `before_after`. An agent looks at results with `edit(render='inline')` / `render` instead. |
| `INKSCAPE_MCP_ENGINE_MODE` | `per_call` | Engine transport for render/export/path/boolean/action-chain (ADR-007). `per_call` spawns a fresh Inkscape per call (default, always correct); `shell` routes those ops through one warm, long-lived `inkscape --shell` worker per document with an **automatic per-call fallback** on any fault. Faster for multi-op batches; any value other than `shell` floors to `per_call`. A private headless worker, **not** a channel to your live GUI. |
| `INKSCAPE_MCP_ENGINE_MAX_PROCESSES` | `2` | Max concurrent warm shell workers when `engine_mode=shell` (LRU-evicted). Floored at 1. |
| `INKSCAPE_MCP_ENGINE_IDLE_TIMEOUT_S` | `300` | Seconds before an idle warm shell worker is reaped. Floored at 1. |

## Tool reference

Ten tools (nine in the default configuration — `actions` appears only in advanced mode). The
operations that used to be separate tools are **ops** inside `edit`, so a drawing is two or three
calls, not twenty, and `tools/list` is ≈15 KB (down from ≈325 KB for the former 99-tool surface).

| Tool | Signature | What it does |
|---|---|---|
| `open` | `(path? \| svg? \| canvas? \| from_live?, ops?, render?, detail?, approval_token?)` | Start working: returns a `doc_id` and a short summary. Exactly one source — a workspace file, SVG text, a blank `canvas{width,height,viewBox?,background?}`, or `from_live{dest_path}` (save the live Inkscape canvas into the workspace). Optional `ops` make the first edit in the same call (`render='inline'` returns a PNG). |
| `inspect` | `(doc_id \| [doc_id…], include?, select?, detail?, limit?, series?)` | Read-only. `include` ⊂ `summary` (default), `objects`, `tree`, `layers`, `styles`, `fonts`, `assets`, `validate`, `quality`, `snapshots`, `frames`. `select` (`tag`, `fill`, `stroke`, `text`, `id_prefix`, `bbox`) filters `objects` — no separate find call. Objects are compact `[id, tag, bbox, fill]` rows with a `limit` and a `truncated` flag. |
| `edit` | `(doc_id, ops, render?, dry_run?, detail?, approval_token?)` | **Many typed ops in ONE atomic call** (≤ 64, all-or-nothing, one snapshot, one Operation Record). `$<n>` refers to the id op *n* created; ops with targets take `ids` or `select`. `render='inline'` returns a PNG, `dry_run=true` the plan, `detail=ids\|summary\|full` the amount of output. Risk = max over the ops. |
| `render` | `(doc_id, target?, object_id?, width_px?, series?, label?)` | An inline PNG of the page (`target='page'`), one object (`'object'` + `object_id`) or the next numbered frame of a `series` (`'frame'`, a screenshot log). |
| `export` | `(doc_id \| [doc_id…], targets, out_dir?, name_prefix?, byte_budget?, dry_run?, approval_token?)` | Write artifacts. `targets` mixes `{kind:'file', format:'png\|pdf\|svg', object_id?, width_px?}`, `{kind:'profile', name:'web\|icons\|print', …}` and `{kind:'save_as', dest_path, overwrite?}`. A list of ids exports a set; `dry_run` projects sizes without writing; `byte_budget` caps the total. |
| `history` | `(doc_id, action?, snapshot_id?, label?)` | `action` = `list` · `create` (checkpoint, optional `label`) · `restore` (roll back; itself reversible) · `prune` (reclaim old snapshots and live frames) · `reload` (re-read the source file under the same `doc_id`). |
| `help` | `(goal? \| op? \| topic? \| stat_paths?)` | `goal='make the svg smaller'` → the `tool:op` to use + an example (flags out-of-scope goals); `op='fill'` → fields, risk and a validated example; `topic='ops'` lists every op, `'capabilities'` the runtime matrix, `'diagnose'` re-probes it; `stat_paths=[…]` returns size + sha256 of workspace files. No argument: the short workflow. |
| `live` | `(action, prefer?, include?, region?, scale?, fast?, timeout_s?, mode?, zoom?, …, operation_id?)` | Look at and steer the view of a **running** Inkscape. `observe` (`include` ⊂ `document`, `selection`, `inspect`, `scene`, `view`) returns those parts in one call (`view` = an inline PNG) and connects automatically; `wait` blocks until something changes; `viewport` moves the view; `diff` shows what a `live_edit` changed; `export_selection`. Setup: `check`, `connect`, `disconnect`, `status`, `install_helper`, `arm_socket`. Hidden when `INKSCAPE_MCP_LIVE_ENABLED` is off. |
| `live_edit` | `(action, style?, transform?, svg_fragment?, text?, observe?, approval_token?)` | Change the live selection: `apply` a `style{fill,stroke,stroke_width,opacity}` and/or `transform{dx,dy,scale,rotate}`, `insert_svg`, or `set_text`. `observe` = `none\|diff\|render`. Always HIGH risk: needs `approval_token`. Hidden when live mode is off. |
| `actions` | `(action, steps?, doc_id?, all?, dry_run?, approval_token?)` | *Advanced mode only* (`INKSCAPE_MCP_RAW_ACTION_ENABLED=1`). Allow-listed Inkscape Actions: `list`, `extensions`, `validate` a chain of `{action, args}` steps, `run` it (HIGH), `raw` for one step. Prefer an `edit` op whenever one exists. |

### Edit ops

An op is `{"op": NAME, …fields}`. `help(op=NAME)` shows its fields, risk and a valid example;
`help(topic='ops')` and the `inkscape://ops` resource list them all. Rules shared by all ops:
a create-op takes an optional `id`; an op with targets takes `ids` or `select`, resolved when the
op runs (so it sees objects created earlier in the same call); `"$<n>"` names the id op *n* created.

| Group | Ops |
|---|---|
| create | `create_rect`, `create_circle`, `create_ellipse`, `create_line`, `create_polygon`, `create_polyline`, `create_path`, `create_text`, `create_group`, `create_use`, `linear_gradient`, `radial_gradient`, `group`, `reparent` |
| style | `fill`, `stroke`, `opacity`, `replace_color`, `palette`, `font` |
| object | `replace_text`, `duplicate`, `rename`, `delete` (HIGH) |
| transform | `move`, `scale`, `rotate`, `tile`, `resize_canvas`, `normalize_viewbox`, `fit_to_content` |
| compose | `set_svg` (HIGH), `insert_svg` (HIGH), `place`, `grid` |
| path *(HIGH, advanced mode)* | `simplify`, `union`, `difference`, `combine`, `break_apart`, `stroke_to_path`, `cleanup` |
| optimize | `optimize_web` |

Example — a whole logo, rendered, in one call:

```jsonc
open(canvas={"width": 400, "height": 400, "background": "#ffffff"},
     ops=[{"op": "create_circle", "cx": 200, "cy": 200, "r": 150, "fill": "#3366ff", "id": "disc"},
          {"op": "create_text", "x": 130, "y": 215, "text": "ink", "fill": "#ffffff"},
          {"op": "group", "ids": ["disc", "$1"], "id": "logo"}],
     render="inline")
export(doc_id, targets=[{"kind": "file", "format": "png", "width_px": 512},
                        {"kind": "file", "format": "svg"}])
```

### Calls, results and errors

- **Compact results.** Every tool returns one compact JSON text block (plus an inline PNG where
  asked) and no `outputSchema`, so nothing is sent twice. The default `edit` result is
  `{"doc_id", "changed", "snapshot_id", "risk", "created": [{"op", "id", "bbox"}], "warnings"}`.
- **Errors say what to do.** `ops[2] fill: id 'logo2' not found (similar: logo, logo-bg)` ·
  `ops[3] delete is HIGH risk; re-call with approval_token` · `document id not found; open ids: d_1a2b`
  · invalid arguments are one line (`invalid export arguments: targets[0].file.format: Field
  required`), never a pydantic dump.
- **Atomic.** Every op is validated before anything is mutated; one failure leaves the document
  byte-identical (the rejected call leaves a `discarded` Operation Record).
- **Paths.** A caller-chosen write target is `dest_path` (a file) or `out_dir` + `name_prefix`; a
  relative path anchors to the **workspace root**, never the process CWD, and is sandbox + symlink
  checked (`path rejected: outside workspace`). Artifacts are returned as workspace-relative paths —
  never an absolute host path (sec.12). PNGs are returned **inline**: view the image, do not `Read`
  a path.
- **`changed` is decided in one place** — the edit pipeline canonical-serializes the document
  before and after. A genuine no-op (a `fill` to the colour already present, a `replace_color`
  matching nothing) reports `changed: false` and writes **no snapshot and no Operation Record**.
- **No hidden renders.** An edit renders nothing unless asked (`render='inline'`).
  `INKSCAPE_MCP_EDIT_PREVIEWS=after|before_after` opts in to per-edit preview frames on the
  Operation Record.

### Risk, annotations and gating

Risk is computed **per call** from its arguments: `edit` / `open` take the maximum over their ops
(`delete`, `set_svg`, `insert_svg` and the path ops are HIGH; everything else medium), `export` is
HIGH only for an overwriting `save_as`, `live_edit` and running `actions` are always HIGH, and
`inspect` / `render` / `help` are low. A HIGH call without a non-empty `approval_token` is refused.
The per-tool ceiling is shown in `llms.txt`.

Every tool carries explicit MCP `ToolAnnotations` from one map
(`src/inkscape_mcp/tool_annotations.py`): `inspect` / `render` / `help` are read-only, `open` is
non-destructive, `edit` / `history` / `export` destructive, `live` open-world, `live_edit` /
`actions` destructive + open-world. Visibility is controlled by exactly two operator flags and one
static name→tag map (`src/inkscape_mcp/tool_tags.py`):

| Flag | Default | Off → hides |
|------|---------|-------------|
| `INKSCAPE_MCP_LIVE_ENABLED` | `true` | `live`, `live_edit` |
| `INKSCAPE_MCP_RAW_ACTION_ENABLED` (advanced mode) | `false` | `actions`, and the path ops are refused |

So the **default** surface (live on, advanced off) is nine tools; advanced mode adds `actions`
(ten); turning live off as well leaves seven. `help(topic='capabilities')` reports the **active**
post-filter surface. The generated `llms.txt` documents the FULL catalog (both flags forced on).

### Live mode — *on by default (operator-chosen)*

Control of a **running** Inkscape, cross-platform via a transport abstraction (extension-socket
bridge on any OS; DBus `org.gtk.Actions` an optional Linux fast-path). Gated by
`INKSCAPE_MCP_LIVE_ENABLED`; absent/unsupported transports are reported cleanly, never as errors.
**No-freeze:** the socket bridge is a *modal* `inkex` effect extension (freezes the GUI for the
whole session); the Linux DBus path runs in Inkscape's own main loop and does **not** freeze the
GUI — `live(action='connect', prefer='no_freeze')` selects it on Linux for viewport, style/transform
writes, and a structured export-to-file read. Windows/macOS live stays modal (best-effort). The
command schema is a fixed enum (wire **protocol v5**) — no arbitrary code or raw Action passthrough
(ADR-003).

The loop is **perceive → decide → act → observe**, one bounded, cancelable step per call (no
server-side autonomous runner, ADR-006):

1. **Perceive** — `live(action='observe', include=['selection', 'inspect', 'view'])`: the
   selection (ids + bboxes), the selected objects' details and a rendered PNG of the canvas in one
   call; `scene` adds the machine-readable `LiveScene` (viewport, canvas size, visible objects) so
   the agent reasons over structure, not pixels. `wait` polls a CHEAP server-hashed state token
   (revision + selection + viewport) on a bounded wait, so the loop reacts to the user's own GUI
   edits without busy-rendering.
2. **Act** — `live_edit(action='apply' | 'insert_svg' | 'set_text', …, observe='render')`. Always
   HIGH: refused without an `approval_token`, and each act produces a Live Operation Record with
   before/after renders (live never mutates unapproved).
3. **Observe** — the rendered canvas, or `live(action='diff', operation_id=…)` for a focused,
   annotated before/after diff (changed bbox + selection outline) linked to the record.

`open(from_live={dest_path})` saves the live document as a new tracked workspace document. The
`live_canvas_assist` **prompt** is the entry point that orients the agent on this loop.

The helper extension can also be installed without the server via the dynamic installers in
[`scripts/`](scripts/): `install-live-helper.sh` (Linux / macOS / Windows under Git Bash or WSL)
and `install-live-helper.ps1` (native Windows PowerShell). Both resolve the Inkscape user
extensions dir at runtime (`inkscape --user-data-directory`, with `INKSCAPE_PROFILE_DIR` and an
OS-aware fallback) and do not require the master gate.

## Resource reference

Read-only MCP resources addressable by URI. Document resources are templated on `doc_id`.

| URI | Description |
|---|---|
| `inkscape://ops` | The complete edit-op reference, generated from the op union: groups, and per op its fields, risk and a validated example (the same data as `help(op=…)`). |
| `inkscape://runtime/capabilities` | Cached runtime capability matrix, including the authoritative MCP tool surface (`tool_count` + `tools:[{name, purpose, risk}]`, from the live registry). |
| `inkscape://runtime/intents` | Curated goal→`(tool, op)` intent map (`[{goal_pattern, targets, how_to, group, example}]`) — the same map `help(goal=…)` uses, without the full capabilities payload. |
| `inkscape://documents` | Index of open documents and their concrete per-doc resource URIs (discoverable via `ListMcpResourcesTool`). |
| `inkscape://prompts` | Index of registered MCP prompts (name + one-line purpose + arguments, from the live `mcp.list_prompts()` registry), so the prompt library is discoverable via `ListMcpResourcesTool` (prompts are otherwise a separate MCP capability the resource surface can't see); fetch a prompt's text via the MCP prompts API. |
| `inkscape://document/{doc_id}/summary` | Top-level document summary. |
| `inkscape://document/{doc_id}/tree` | Element tree. |
| `inkscape://document/{doc_id}/layers` | Layer list. |
| `inkscape://document/{doc_id}/objects` | Object inventory. |
| `inkscape://document/{doc_id}/styles` | Style usage. |
| `inkscape://document/{doc_id}/fonts` | Fonts referenced. |
| `inkscape://document/{doc_id}/assets` | External assets / references. |
| `inkscape://live/session` | Live-session state (enabled / connected / transport). Clean when no session. |
| `inkscape://live/selection` | Current live selection (object ids). Empty when no session. |
| `inkscape://live/view` | Current live frame's structured metadata: the `LiveScene` (selection ids + bboxes, viewport, canvas size, visible-object summary), without PNG bytes. Empty when no session. |
| `inkscape://live/events` | Latest live change state: the current cheap state token + classified deltas (`selection_changed` / `document_changed` / `viewport_changed`). Read-only; empty `LiveChange` when no session. |
| `inkscape://live/operations` | Recent Live Operation Records: what each mutation changed, approval, before/after renders. Paths are workspace-relative or opaque (`<external>`) — never a host path — and cleared at each session boundary. Empty when none. |

## Prompt reference

MCP Prompts orient the agent on how to use the tool surface safely; they grant no capability of
their own (architecture §4.1).

| Prompt | Args | Description |
|---|---|---|
| `live_canvas_assist` | `(goal)` | Entry point for the live-view co-pilot loop. Orients the agent to drive a running Inkscape toward `goal`, one bounded perceive→decide→act→observe step at a time: `live(action='observe', …)`, ONE semantic act via the approval-gated `live_edit`, then the rendered canvas or `live(action='diff')`; `live(action='wait')` reacts to user edits. Acts are semantic-only and approval-gated and the loop is bounded/cancelable — zero new authority. |
| `prepare_web_export` | `()` | Orients the agent on producing web-ready assets (optionally quality-check + `optimize_web` first, then `export` with the `web` profile and file targets). Guidance only. |
| `prepare_icon_set` | `()` | Orients the agent on producing a multi-size square PNG icon set via `export` with the `icons` profile. Guidance only. |
| `prepare_print_export` | `()` | Orients the agent on producing a print-ready vector PDF via `export` with the `print` profile (validate fonts/assets first). Guidance only. |
| `theme_recoloring` | `()` | Orients the agent on recoloring to a brand/theme palette via the `replace_color` / `palette` ops (validated, reversible). Guidance only. |
| `compose_artwork` | `(goal)` | On-ramps the generative loop toward `goal`: `open(canvas=…, ops=[…], render='inline')` → one `edit` call of shape/gradient/`fill`/group ops with `$n` references → look → `inspect(include=['validate'])` → `export`, with `history` reversibility. Guidance only. |
| `restyle_artwork` | `(goal)` | On-ramps the OBJECT-TARGETED restyle loop toward `goal`: `inspect(select=…)` to address objects → `fill` / `stroke` / `opacity` / `font` ops with `ids` or `select` in one `edit` call → look → `export`; companion to the document-wide `theme_recoloring`. Guidance only. |

## Try asking your agent to…

Natural-language asks and the `tool:op` each exercises. This catalog is **aligned with the same
curated goal→`(tool, op)` map** that powers `help(goal=…)` and the `intents` section of
`help(topic='capabilities')` ([`src/inkscape_mcp/intents.py`](src/inkscape_mcp/intents.py)) — a
test checks that every target and example call named there exists, so the doc, the discovery tool,
and the runtime matrix never diverge. Don't know which call fits? Ask `help(goal="…")` in words and
it returns the same mapping plus an example. For the full open→edit→export workflow,
snapshots/reversibility, and the risk/approval model see the
[agent-usage guide](docs/agent-usage-guide.md).

**Generate / draw** — all are `edit` ops and can share one call

- "Draw a rectangle / circle / line on a new canvas." → `open:canvas`, `edit:create_rect`,
  `edit:create_circle`, `edit:create_line`
- "Add a text label." → `edit:create_text`
- "Add a gradient fill (linear or radial)." → `edit:linear_gradient`, `edit:radial_gradient`, `edit:fill`
- "Group these objects together." → `edit:group`, `edit:create_group`
- "Lay out a 12-icon system as a contact/spec sheet in one call." → `edit:grid`, `edit:place`

**Edit**

- "Make this shape blue / change its fill." → `edit:fill`
- "Change the stroke / outline and opacity." → `edit:stroke`, `edit:opacity`
- "Swap one colour for another across the whole document." → `edit:replace_color`, `edit:palette`
- "Move / scale / rotate an object." → `edit:move`, `edit:scale`, `edit:rotate`
- "Delete these objects." (HIGH-risk, approval-gated, reversible) → `edit:delete`
- "Resize the canvas or fit it to the content." → `edit:resize_canvas`, `edit:fit_to_content`,
  `edit:normalize_viewbox`
- "Simplify / clean up these paths." (HIGH-risk, advanced mode, dry-run first) → `edit:simplify`,
  `edit:cleanup`
- "Union / subtract these shapes." (HIGH-risk, advanced mode) → `edit:union`, `edit:difference`,
  `edit:combine`

**Inspect**

- "Open this SVG and tell me what's in it." → `open:path`, `inspect:summary`, `inspect:tree`
- "Find the red shapes / all text / objects by id." → `inspect:objects` (with `select`)
- "Is this document valid? Give me a quality report." → `inspect:validate`, `inspect:quality`
- "Show me what it looks like now." → `render:page`
- "What's the byte size / sha256 of this file (or this set)?" → `help` with `stat_paths`
- "What can this server do? Why isn't Inkscape working?" → `help:capabilities`, `help:diagnose`

**Export**

- "Export a 512 px PNG." → `export:file`
- "Export just this one object." → `export:file` with `object_id`
- "Export a whole icon set / many sizes at once." → `export:profile` (`icons`), several `export:file`
  targets in one call
- "Export web-ready / print-ready assets." → `export:profile` (`web`, `print`)
- "Make the SVG smaller for the web." → `edit:optimize_web`
- "Save it to a new file." → `export:save_as`

**History and live**

- "Snapshot / undo / restore the document state." → `history:create`, `history:list`, `history:restore`
- "Connect to my running Inkscape and work on the open canvas." → `live:observe`, `live_edit:apply`

## Safety model

The server is built to be safe to hand to an autonomous agent:

- **Workspace sandbox.** Every path is normalized and resolved; symlinks are guarded; access
  outside the configured workspace root(s) is rejected before any I/O.
- **Originals untouched.** Documents are opened as working copies. No tool overwrites the source;
  `export` with a `save_as` target writes elsewhere, and overwriting an existing file needs explicit
  approval.
- **Reversibility.** Every mutating call snapshots first and emits one Operation Record;
  `history(action='restore')` rolls the working copy back to any indexed snapshot. An explicit
  retention sweep (boot-time + `history(action='prune')`) bounds snapshot growth without ever
  pruning the baseline or the live head.
- **Risk policy.** `low`/`medium` are permitted; `high` (the risk of an `edit` call is the maximum
  over its ops) requires a per-operation `approval_token`
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
  quality.py    # read-only quality report (wraps validate + inspect + optimizer counts)
  logging_setup.py # stderr-only structured logging (stdout reserved for MCP STDIO)
  document/     # direct-DOM inspection engine (summary/tree/layers/styles/fonts/assets)
  edit/         # safe-edit engines: dom.py (lxml primitives + shared SAFE_ID_RE), pipeline.py
                #   (snapshot + optional preview + Operation Record wrapper; risk-classed),
                #   ops.py (the op union + kernel behind `edit`/`open`: validation, `$n` refs,
                #   selectors, dry-run plan, atomic apply), op_docs.py (per-op docs + validated
                #   examples → `help(op=…)`, `inkscape://ops`, llms.txt), style.py,
                #   text_object.py, transform.py, create.py (element-creation + defs/gradients +
                #   grouping engines), collection.py (place/grid), compose.py (set/insert SVG),
                #   optimize.py (web-optimize: strip editor cruft + drop dead structure + reduce
                #   coord precision; reversible), paths.py (HIGH-risk path geometry via the
                #   Inkscape engine — arg-list Actions → safe-parse → DOM-replace)
  actions/      # controlled Action surface: capability_map.py (version-keyed Action map +
                #   discovery, persisted under action-maps/), chains.py (typed ActionStep chains →
                #   allowlist+map validation → arg-list --actions argv → engine; no raw passthrough;
                #   behind the advanced `actions` tool)
  render/       # Inkscape CLI render/export engine (cli.py) + export profiles (profiles.py) +
                # bounded typed batch export (batch.py: item cap + byte budget + dry-run) +
                # in-process content-truth verifier (verify.py: PDF is_vector/fonts_outlined,
                #   raster opaque_px/all_blank — no pdffonts/Pillow subprocess)
  engine/       # ADR-007 opt-in warm `inkscape --shell` engine: process.py (one supervised
                #   worker — read-until-prompt framing, per-command timeout+kill, crash/idle reap),
                #   manager.py (per-working-copy worker pool, serialized, LRU, freshness reopen),
                #   ops.py (shell export/action composition). Gated by INKSCAPE_MCP_ENGINE_MODE=shell
                #   with an automatic per-call CLI fallback; render/paths/chains route through it.
  live/         # live read + live write + live view (the `live` / `live_edit` tools): transport ABC + capability-aware
                #   backend selection, extension-socket + DBus backends, protocol.py wire schema
                #   (v5: read + semantic write + view-only commands, region/scale render, state token),
                #   session manager, render/sync, edit.py (write + view engine reusing semantics
                #   + bounded view validators), records.py (Live Operation Records + approval gate),
                #   scene.py (LiveScene), diff.py (focused visual diff),
                #   cache.py (bounded LRU render cache keyed (doc_revision, viewport, scale) +
                #   coalescing budget; freshness via the revision key), helper_extension/ (runs inside
                #   Inkscape). Gate on by default.
  prompts/      # MCP Prompts (architecture §4.1): live.py (live_canvas_assist — live-view loop entry
                #   point), library.py (export/recolor orientation prompts), authoring.py
                #   (compose_artwork / restyle_artwork — generative on-ramp prompts)
  resources/    # MCP resource templates (runtime caps, document/{id}/..., live/{session,selection,operations})
  runtime/      # Inkscape capability probe
  tools/        # the ten @mcp.tool modules: open_, inspect_, edit, render, export, history,
                #   help_, live, live_edit, actions; _result.py (compact results + the ONE
                #   exception→ToolError map), _middleware.py (one-line argument-validation errors)
  intents.py    # curated goal → (tool, op) discovery map + out-of-scope detector (help(goal=…))
  overview.py   # the in-context MCP `instructions` (flow, ops, risk, reversibility, `help`)
  validation_errors.py # one-line rendering of pydantic argument errors (shared by parse_ops + middleware)
  schema_compact.py # post-registration pass that shrinks the tools/list input schemas
  tool_tags.py / tool_annotations.py # name→tag gating map + explicit MCP annotations per tool
  workspace/    # sandbox/path safety, limits, risk policy, safe XML parse, subprocess wrapper
scripts/        # install-live-helper.sh (Linux/macOS/Git-Bash) + .ps1 (Windows): copy the live
                #   helper extension into Inkscape's user extensions dir, resolved dynamically;
                #   gen_llms_txt.py: regenerate llms.txt / llms-full.txt from the live registry
llms.txt        # GENERATED LLM index (one line + risk class per tool); do not hand-edit
llms-full.txt   # GENERATED full manifest (full descriptions + key params + the op reference + prompts
                #   + resources)
evals/          # deterministic tool-usability harness: tool_selection_scenarios.json +
                #   run_eval.py (drives help(goal=…)/intents; scores `tool:op` targets), run_call_count.py
                #   (calls + bytes for three reference scenarios). ruff-checked.
                #   run_live_eval.py: OPTIONAL report-only live-agent runner (same dataset/scorer).
examples/       # ready-to-copy MCP host configs (claude_desktop_config.json, mcp.json)
tests/          # pytest; Inkscape-dependent tests marked @pytest.mark.inkscape
```

Install / config / compatibility / troubleshooting docs: [`docs/install/`](docs/install/).
Driving the server from an agent (open→edit→export loop, ops, snapshots, risk/approval gate, finding
the right call): [`docs/agent-usage-guide.md`](docs/agent-usage-guide.md).

## Development

- **Stack:** Python ≥ 3.12 · `uv` · FastMCP 3.x · `lxml` · Pillow (live visual diff) · Inkscape CLI (per-call, plus an opt-in warm `inkscape --shell` engine — ADR-007) · STDIO transport.
- **Conventions:** few result-oriented tools with typed ops (no portmanteau / `run_action(string)`),
  risk-classed, mutating calls emit Operation Records and never overwrite originals, subprocess via
  arg-lists.
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
