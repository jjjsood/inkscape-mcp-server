# inkscape-mcp

**A Model Context Protocol server that lets AI agents inspect, edit, validate, render, and export
SVG documents — safely, reversibly, and without a GUI.**

[![CI](https://github.com/jjjsood/inkscape-mcp/actions/workflows/ci.yml/badge.svg)](.github/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-%E2%89%A53.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastMCP](https://img.shields.io/badge/MCP-FastMCP%203.x-5A45FF)](https://github.com/jlowin/fastmcp)
[![Inkscape](https://img.shields.io/badge/Inkscape-1.4.x-000000?logo=inkscape&logoColor=white)](https://inkscape.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

LLM agents are good at deciding *what* should change in a drawing — "make the logo blue, set the
heading to 24 px, export a 512 px icon" — but poking at raw XML or driving a GUI is error-prone,
and a free-form "run this Inkscape command" tool can overwrite originals or corrupt files with no
way back.

`inkscape-mcp` gives agents a **small, typed, risk-classed API** instead:

- **Ten result-oriented tools.** `open` → `edit` → `export`, plus `inspect`, `render`, `history`,
  `help`, and optional live-mode tools. A typical drawing takes two or three calls.
- **Atomic batch edits.** One `edit` call applies up to 64 typed ops all-or-nothing, can reference
  objects created earlier in the same call, and returns a rendered preview on request.
- **Reversible by construction.** Every change runs on a working copy, takes a snapshot, and writes
  an Operation Record. Any state can be restored; originals are never modified.
- **Safe to hand to an agent.** Workspace sandbox, risk classes with approval-gated high-risk
  operations, safe XML parsing, resource limits, and no shell strings, network access, or arbitrary
  extension execution.
- **Headless-first, live-capable.** Works without a GUI; optionally observes and steers a running
  Inkscape instance on Linux, macOS, and Windows.

## Contents

- [Quickstart](#quickstart)
- [Usage](#usage)
- [Tools](#tools)
- [Edit ops](#edit-ops)
- [Live mode](#live-mode)
- [Resources and prompts](#resources-and-prompts)
- [Configuration](#configuration)
- [Security model](#security-model)
- [Development](#development)
- [Documentation](#documentation)
- [License](#license)

## Quickstart

### Requirements

- Python **3.12+** and [`uv`](https://docs.astral.sh/uv/) (or `pipx`)
- [Inkscape](https://inkscape.org/) **1.4.x** on `PATH` — needed for render, export, and geometry
  ops; reading and DOM editing work without it
- A directory of SVG files to use as the workspace root

### Install

> `inkscape-mcp` is not yet published on PyPI. Install it from git or a local checkout.

```bash
# Run without installing
uvx --from "git+https://github.com/jjjsood/inkscape-mcp.git" inkscape-mcp

# Or install the console script persistently
pipx install "git+https://github.com/jjjsood/inkscape-mcp.git"
```

The server speaks MCP over **STDIO** and waits for a client to connect.

### Connect an MCP client

Add the server to your client's MCP configuration (Claude Desktop, Claude Code, or any
STDIO-capable host):

```json
{
  "mcpServers": {
    "inkscape": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/jjjsood/inkscape-mcp.git", "inkscape-mcp"],
      "env": {
        "INKSCAPE_MCP_WORKSPACE_ROOTS": "/absolute/path/to/your/svgs"
      }
    }
  }
}
```

With a `pipx` install, use `"command": "inkscape-mcp"` and `"args": []`. For Claude Code:

```bash
claude mcp add inkscape \
  --env INKSCAPE_MCP_WORKSPACE_ROOTS=/absolute/path/to/your/svgs \
  -- inkscape-mcp
```

Ready-to-copy configs are in [`examples/`](examples/); per-host instructions are in
[`docs/install/host-configs.md`](docs/install/host-configs.md).

### Verify

Ask your agent to call `help(topic='diagnose')`. It reports the detected Inkscape version, export
formats, and live-mode availability.

## Usage

The core flow is **open → edit → look → export**. A complete logo, rendered and exported, is two
calls:

```python
open(canvas={"width": 400, "height": 400, "background": "#ffffff"},
     ops=[{"op": "create_circle", "cx": 200, "cy": 200, "r": 150, "fill": "#3366ff", "id": "disc"},
          {"op": "create_text", "x": 130, "y": 215, "text": "ink", "fill": "#ffffff"},
          {"op": "group", "ids": ["disc", "$1"], "id": "logo"}],
     render="inline")

export(doc_id, targets=[{"kind": "file", "format": "png", "width_px": 512},
                        {"kind": "file", "format": "svg"}])
```

`"$1"` refers to the id created by op 1 in the same call. Ops that act on existing objects take
explicit `ids` or a `select` filter (`fill`, `stroke`, `tag`, `text`, `id_prefix`, `bbox`).

Some things to ask an agent:

| Ask | Calls |
|---|---|
| "Open this SVG and tell me what's in it." | `open`, `inspect` |
| "Make every red shape blue." | `edit` → `replace_color` |
| "Find all text and set it in Inter, 24 px." | `edit` → `font` with `select` |
| "Lay out these 12 icons as a spec sheet." | `edit` → `grid`, `place` |
| "Is this file valid? Give me a quality report." | `inspect(include=['validate', 'quality'])` |
| "Make the SVG smaller for the web." | `edit` → `optimize_web` |
| "Export an icon set in all sizes." | `export` → `icons` profile |
| "Undo the last change." | `history(action='restore')` |
| "Recolor the selection in my open Inkscape." | `live`, `live_edit` |

Not sure which call fits? `help(goal="make the svg smaller")` returns the matching tool and op with
an example. The [agent usage guide](docs/agent-usage-guide.md) covers the full workflow.

## Tools

| Tool | Purpose |
|---|---|
| `open` | Open a workspace file, SVG text, a blank canvas, or the live canvas. Returns a `doc_id`; can apply initial ops and render in the same call. |
| `inspect` | Read-only: summary, objects, tree, layers, styles, fonts, assets, validation, quality report, snapshots. Supports `select` filters. |
| `edit` | Apply many typed ops in one atomic call. Supports `$n` references, `dry_run`, and `render='inline'`. |
| `render` | Return an inline PNG of the page, one object, or the next frame of a numbered series. |
| `export` | Write PNG/PDF/SVG files, export profiles (`web`, `icons`, `print`), or `save_as` a new file. Supports batches, dry runs, and byte budgets. |
| `history` | List, checkpoint, restore, or prune snapshots; reload the source file. |
| `help` | Goal-to-call lookup, per-op reference with examples, capability matrix, runtime diagnostics. |
| `live` | Observe and navigate a running Inkscape: selection, scene, rendered view, change waits, visual diffs. *Live mode.* |
| `live_edit` | Apply style, transform, SVG, or text changes to the live selection. Always high-risk. *Live mode.* |
| `actions` | Run allow-listed Inkscape Actions. *Advanced mode only.* |

The default surface is nine tools; `actions` appears only when advanced mode is enabled.

**Results** are compact JSON, plus an inline PNG where requested. **Errors** state what to do
next, e.g. `ops[2] fill: id 'logo2' not found (similar: logo, logo-bg)`. A call that fails
validation leaves the document byte-identical, and an edit that changes nothing reports
`changed: false` without writing a snapshot.

## Edit ops

An op is `{"op": NAME, ...fields}`. The `edit` tool's `ops` field lists every op with its fields;
`help(op=NAME)` adds a validated example, `help(topic='ops')` / the `inkscape://ops` resource the
full list.

| Group | Ops |
|---|---|
| Create | `create_rect`, `create_circle`, `create_ellipse`, `create_line`, `create_polygon`, `create_polyline`, `create_path`, `create_text`, `create_group`, `create_use`, `linear_gradient`, `radial_gradient`, `group`, `reparent` |
| Style | `fill`, `stroke`, `opacity`, `replace_color`, `palette`, `font` |
| Object | `replace_text`, `duplicate`, `rename`, `delete`¹ |
| Transform | `move`, `scale`, `rotate`, `tile`, `resize_canvas`, `normalize_viewbox`, `fit_to_content` |
| Compose | `set_svg`¹, `insert_svg`¹, `place`, `grid` |
| Path² | `simplify`, `union`, `difference`, `combine`, `break_apart`, `stroke_to_path`, `cleanup` |
| Optimize | `optimize_web` |

¹ High-risk: requires an `approval_token`. ² High-risk and available only in advanced mode.

## Live mode

Live mode connects to a **running Inkscape** and lets an agent work alongside you in a bounded
*perceive → act → observe* loop: one step per call, no autonomous background runner.

1. **Perceive** — `live(action='observe', include=['selection', 'inspect', 'view'])` returns the
   selection, object details, and a rendered view in one call. `live(action='wait')` blocks until
   you change something in the GUI.
2. **Act** — `live_edit(action='apply', style=..., transform=...)`. Every live edit is high-risk,
   requires an `approval_token`, and produces a Live Operation Record with before/after renders.
3. **Observe** — the rendered result, or `live(action='diff')` for an annotated before/after diff.

Live mode uses a helper Inkscape extension over a loopback socket on all platforms, with DBus as an
optional Linux fast path that does not freeze the GUI (`live(action='connect',
prefer='no_freeze')`). The command protocol is a fixed schema — no arbitrary code or raw Action
passthrough. Install the helper with `live(action='install_helper')` or the standalone scripts
[`scripts/install-live-helper.sh`](scripts/install-live-helper.sh) /
[`scripts/install-live-helper.ps1`](scripts/install-live-helper.ps1).

Live mode is enabled by default; set `INKSCAPE_MCP_LIVE_ENABLED=false` to hide `live` and
`live_edit`.

## Resources and prompts

<details>
<summary><strong>MCP resources</strong> (17 read-only URIs)</summary>

| URI | Description |
|---|---|
| `inkscape://ops` | Complete edit-op reference: fields, risk, and a validated example per op. |
| `inkscape://runtime/capabilities` | Runtime capability matrix, including the active tool surface. |
| `inkscape://runtime/intents` | Goal → `(tool, op)` map used by `help(goal=...)`. |
| `inkscape://documents` | Open documents and their per-document resource URIs. |
| `inkscape://prompts` | Registered prompts with purpose and arguments. |
| `inkscape://document/{doc_id}/summary` | Document summary. |
| `inkscape://document/{doc_id}/tree` | Element tree. |
| `inkscape://document/{doc_id}/layers` | Layers. |
| `inkscape://document/{doc_id}/objects` | Object inventory. |
| `inkscape://document/{doc_id}/styles` | Style usage. |
| `inkscape://document/{doc_id}/fonts` | Referenced fonts. |
| `inkscape://document/{doc_id}/assets` | External assets and references. |
| `inkscape://live/session` | Live-session state: enabled, connected, transport. |
| `inkscape://live/selection` | Current live selection. |
| `inkscape://live/view` | Structured live scene: selection, viewport, canvas size, visible objects. |
| `inkscape://live/events` | Latest live change state and classified deltas. |
| `inkscape://live/operations` | Recent Live Operation Records. |

</details>

<details>
<summary><strong>MCP prompts</strong> (7 workflow guides)</summary>

Prompts orient the agent on a workflow; they grant no additional capability.

| Prompt | Args | Purpose |
|---|---|---|
| `compose_artwork` | `goal` | Generate new artwork: canvas → ops → look → validate → export. |
| `restyle_artwork` | `goal` | Restyle targeted objects via `select` and style ops. |
| `theme_recoloring` | — | Recolor a document to a brand or theme palette. |
| `prepare_web_export` | — | Optimize and export web-ready assets. |
| `prepare_icon_set` | — | Export a multi-size square PNG icon set. |
| `prepare_print_export` | — | Validate fonts/assets and export a print-ready vector PDF. |
| `live_canvas_assist` | `goal` | Entry point for the live perceive → act → observe loop. |

</details>

## Configuration

All configuration is via environment variables. Only the workspace root is required.

| Variable | Default | Description |
|---|---|---|
| `INKSCAPE_MCP_WORKSPACE_ROOTS` | — | **Required.** Directories the server may read and write, separated by the OS path separator. Everything outside is rejected. |
| `INKSCAPE_MCP_LIVE_ENABLED` | `true` | Enable live mode (`live`, `live_edit`). |
| `INKSCAPE_MCP_RAW_ACTION_ENABLED` | `false` | Enable advanced mode: the `actions` tool and path ops. Does not widen the Action allowlist. |
| `INKSCAPE_MCP_ENGINE_MODE` | `per_call` | `per_call` spawns Inkscape per call; `shell` reuses a warm `inkscape --shell` worker per document, with automatic fallback. |
| `INKSCAPE_MCP_PROCESS_TIMEOUT_S` | `60` | Timeout per Inkscape process, in seconds. |
| `INKSCAPE_MCP_MAX_EXPORT_PX` | `8192` | Maximum raster dimension for render and export. |

<details>
<summary>All settings</summary>

| Variable | Default | Description |
|---|---|---|
| `INKSCAPE_MCP_MAX_INPUT_BYTES` | `52428800` (50 MiB) | Maximum input SVG size. |
| `INKSCAPE_MCP_MAX_OUTPUT_BYTES` | `104857600` (100 MiB) | Maximum size of a produced artifact. |
| `INKSCAPE_MCP_MAX_PROCS` | `2` | Maximum concurrent Inkscape subprocesses. |
| `INKSCAPE_MCP_SNAPSHOT_KEEP_N` | `50` | Snapshots retained per document. |
| `INKSCAPE_MCP_SNAPSHOT_KEEP_DAYS` | `30` | Snapshot age retention, in days. |
| `INKSCAPE_MCP_SNAPSHOT_HARD_MAX_N` | `500` | Hard cap on snapshots per document. |
| `INKSCAPE_MCP_SNAPSHOT_HARD_MAX_BYTES` | `5368709120` (5 GiB) | Hard cap on snapshot bytes. |
| `INKSCAPE_MCP_ARTIFACT_KEEP_DAYS` | `14` | Artifact age retention, in days. |
| `INKSCAPE_MCP_ARTIFACT_MAX_BYTES` | `2147483648` (2 GiB) | Total artifact byte budget. |
| `INKSCAPE_MCP_ARTIFACT_MAX_BYTES_PER_DOC` | `536870912` (512 MiB) | Per-document artifact byte budget. |
| `INKSCAPE_MCP_EDIT_PREVIEWS` | `none` | Preview frames stored on each Operation Record: `none`, `after`, or `before_after`. |
| `INKSCAPE_MCP_ENGINE_MAX_PROCESSES` | `2` | Maximum warm shell workers in `shell` mode (LRU-evicted). |
| `INKSCAPE_MCP_ENGINE_IDLE_TIMEOUT_S` | `300` | Idle timeout for warm shell workers, in seconds. |
| `INKSCAPE_MCP_LIVE_RENDEZVOUS` | — | Explicit path to the live helper's rendezvous file (otherwise auto-discovered). |
| `INKSCAPE_MCP_LIVE_CACHE_MAX_ENTRIES` | `64` | Maximum frames in the live render cache (LRU). |
| `INKSCAPE_MCP_LIVE_CACHE_MAX_BYTES` | `268435456` (256 MiB) | Byte budget for the live render cache. |
| `INKSCAPE_MCP_LIVE_COALESCE_BUDGET_MS` | `200` | Window in which an identical live render reuses the cached frame. `0` disables. |
| `INKSCAPE_MCP_LIVE_FRAME_KEEP_DAYS` | `7` | Age retention for live render frames. |
| `INKSCAPE_MCP_LIVE_FRAME_MAX_BYTES` | `536870912` (512 MiB) | Byte budget for live render frames. |
| `INKSCAPE_MCP_ACTION_ALLOWLIST` | built-in | Extra Inkscape Action ids added to the built-in allowlist. Cannot remove defaults. |
| `INKSCAPE_MCP_EXTENSION_ALLOWLIST` | empty | Inkscape extension ids allowed to execute. |

Retention limits are applied by an explicit sweep at startup and on `history(action='prune')`,
never implicitly during edits.

</details>

## Security model

`inkscape-mcp` is designed to be handed to an autonomous agent.

| Risk class | Examples | Policy |
|---|---|---|
| **Low** | `inspect`, `render`, `help`, `export` to new files | Allowed |
| **Medium** | Create, style, text, and transform ops | Allowed; snapshot-backed and reversible |
| **High** | `delete`, `set_svg`, `insert_svg`, path ops, file overwrites, `live_edit`, running Actions | Requires a per-operation `approval_token` minted out of band |
| **Restricted** | Code execution, network, filesystem escape | Not available |

A call's risk is the highest risk of its ops.

- **Workspace sandbox** — paths are normalized, symlinks are guarded, and anything outside the
  configured roots is rejected before any I/O. Returned paths are workspace-relative.
- **Originals untouched** — documents open as working copies; saving writes a new file, and
  overwriting requires approval.
- **Reversible** — each mutating call takes one snapshot and writes one Operation Record.
- **Subprocess hygiene** — Inkscape is invoked with argument lists, never shell strings; ids and
  formats are validated before reaching the binary.
- **Safe XML** — entity expansion and external entities are disabled (no XXE or billion-laughs).
- **Bounded** — size limits, per-process timeouts, and a concurrency cap. No network access, no
  arbitrary extension execution.
- **Clean errors** — public messages contain no host paths; details go to stderr only.

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Development

```bash
git clone https://github.com/jjjsood/inkscape-mcp.git
cd inkscape-mcp
uv sync                              # runtime + dev dependencies
uv run inkscape-mcp                  # start the STDIO server

uv run pytest                        # test suite
uv run pytest -m "not inkscape"      # skip tests that need the Inkscape binary
uv run ruff check --fix . && uv run ruff format .
uv run mypy src                      # strict type checking
```

Tests that need a real Inkscape binary are marked `@pytest.mark.inkscape` and skip automatically
when `inkscape` is not on `PATH`.

**CI** runs lint, format, type checks, and tests on Linux, macOS, and Windows; the full
real-Inkscape suite on Linux; a packaged-install boot smoke test; and a full-surface MCP smoke test
that checks the registered tools, prompts, and resources.

**Evals** in [`evals/`](evals/) measure tool usability without a live LLM: `run_eval.py` scores
tool-selection accuracy over a labelled set of natural-language requests, and `run_call_count.py`
measures calls and response bytes for reference scenarios.

```bash
uv run python evals/run_eval.py
```

**`llms.txt`** and **`llms-full.txt`** are generated from the live registry by
`scripts/gen_llms_txt.py` — do not edit them by hand.

<details>
<summary>Project layout</summary>

```
src/inkscape_mcp/
  server.py       FastMCP app and STDIO entry point
  config.py       Environment-driven settings and limits
  tools/          The ten MCP tools, result formatting, error mapping
  edit/           Op union and atomic edit pipeline; DOM, style, transform, compose, path engines
  document/       Read-only document inspection
  render/         Inkscape CLI render/export, profiles, batch export, output verification
  engine/         Optional warm `inkscape --shell` worker pool
  actions/        Allow-listed Inkscape Action chains (advanced mode)
  live/           Live-mode transports (socket, DBus), protocol, session, diff, helper extension
  resources/      MCP resources
  prompts/        MCP prompts
  workspace/      Sandbox, path safety, limits, risk policy, safe XML, subprocess wrapper
  registry.py     doc_id ↔ working-copy registry
  snapshots.py    Snapshot engine and restore
  operations.py   Operation Records
  retention.py    Snapshot and frame retention
  validate.py     Validation engine
  quality.py      Quality report
  intents.py      Goal → (tool, op) discovery map
docs/             Installation guides and agent usage guide
evals/            Tool-usability evaluation harness
examples/         MCP host configurations
scripts/          Live-helper installers, llms.txt generator, CI helpers
tests/            pytest suite (tests/e2e/: long real-Inkscape agent workflows)
```

</details>

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines and project conventions.

## Documentation

- [Installation](docs/install/install.md) — `uvx`, `pipx`, and source installs
- [Host configurations](docs/install/host-configs.md) — Claude Desktop, Claude Code, generic STDIO
- [Compatibility matrix](docs/install/compatibility.md) — platforms, Inkscape versions, live transports
- [Troubleshooting](docs/install/troubleshooting.md)
- [Agent usage guide](docs/agent-usage-guide.md) — workflow, ops, snapshots, approvals
- [Changelog](CHANGELOG.md)

## License

[MIT](LICENSE) © Johannes Sood
