# MCP host configuration

The server speaks **MCP over STDIO only**. Every host launches the `inkscape-mcp` console script and
talks JSON-RPC over its stdin/stdout. The only **required** setting is
`INKSCAPE_MCP_WORKSPACE_ROOTS` — without a workspace root the sandbox has nothing it is allowed to
touch.

Ready-to-copy example files live in [`examples/`](../../examples/):

- [`claude_desktop_config.json`](../../examples/claude_desktop_config.json)
- [`mcp.json`](../../examples/mcp.json) (Claude Code / generic `.mcp.json`)

> Replace every `/absolute/path/...` placeholder with a real **absolute** path. STDIO hosts often run
> with a different working directory than your shell, so relative paths and `~` are unreliable.

## Pick a launch command

| Form | `command` | `args` | When |
|---|---|---|---|
| `pipx` install on PATH | `inkscape-mcp` | `[]` | Installed with `pipx install` (script is on PATH). |
| `uvx` from local path | `uvx` | `["--from", "/abs/.../inkscape-mcp", "inkscape-mcp"]` | No persistent install; run from a checkout. |
| `uvx` from git | `uvx` | `["--from", "git+https://github.com/johnnyjagatpal/inkscape-mcp.git", "inkscape-mcp"]` | No checkout; pull from git. |
| `uv` from source | `uv` | `["run", "--directory", "/abs/.../inkscape-mcp", "inkscape-mcp"]` | Development / dogfooding. |

See [install.md](install.md) for the install paths behind each form.

## Claude Desktop

Edit `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`; Windows:
`%APPDATA%\Claude\claude_desktop_config.json`). Add an entry under `mcpServers`:

```json
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

If you used `pipx install`, set `"command": "inkscape-mcp", "args": []` instead. Restart Claude
Desktop after editing.

## Claude Code

Either add a stanza to a project `.mcp.json` (the [example file](../../examples/mcp.json)
is exactly this shape) or use the CLI:

```bash
# pipx install on PATH:
claude mcp add inkscape \
  --env INKSCAPE_MCP_WORKSPACE_ROOTS=/absolute/path/to/your/svgs \
  -- inkscape-mcp

# uvx from a local checkout:
claude mcp add inkscape \
  --env INKSCAPE_MCP_WORKSPACE_ROOTS=/absolute/path/to/your/svgs \
  -- uvx --from /absolute/path/to/inkscape-mcp inkscape-mcp

# uvx straight from git:
claude mcp add inkscape \
  --env INKSCAPE_MCP_WORKSPACE_ROOTS=/absolute/path/to/your/svgs \
  -- uvx --from git+https://github.com/johnnyjagatpal/inkscape-mcp.git inkscape-mcp
```

`.mcp.json` form:

```json
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

The `uv run --directory` form is handy for dogfooding the server straight from a source checkout
once `uv sync` has run.

## Generic STDIO host

Any MCP client that launches a subprocess and speaks JSON-RPC over STDIO works. The contract:

- **command + args** must end up running `inkscape_mcp.server:main` (the `inkscape-mcp` script).
- **stdin/stdout** are the MCP channel — do not write to stdout from wrappers. The server logs to
  **stderr only**.
- **env** must include `INKSCAPE_MCP_WORKSPACE_ROOTS`; Inkscape must be on the subprocess `PATH` for
  render/export/live tools.

Smoke test outside any host:

```bash
INKSCAPE_MCP_WORKSPACE_ROOTS=/abs/path/to/your/svgs inkscape-mcp </dev/null
# boots, sees EOF, exits cleanly = wired correctly
```

## Environment variables

Authoritative list and defaults from [`config.py`](../../src/inkscape_mcp/config.py). All configuration is
environment-driven — there is no config file. List vars are **OS-path-separator-delimited** (`:` on
Linux/macOS, `;` on Windows).

| Env var | Default | Purpose |
|---|---|---|
| `INKSCAPE_MCP_WORKSPACE_ROOTS` | *(none)* | **Required.** Directories the server may read/write. Missing / non-dir / non-read+writable roots are excluded and explained in diagnostics; never auto-created, never falls back to CWD/home. |
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
| `INKSCAPE_MCP_LIVE_ENABLED` | `true` | Master gate for live mode (E3/E4). **On by default**; set a falsy value (`0`/`false`/`no`/`off`) to opt out (then `live_connect` refuses cleanly; headless is unaffected). |
| `INKSCAPE_MCP_LIVE_RENDEZVOUS` | *(none)* | Optional explicit path to the live helper's rendezvous file (otherwise discovered under the Inkscape user data dir / temp dir). |
| `INKSCAPE_MCP_RAW_ACTION_ENABLED` | `false` | Advanced-mode gate for the `run_raw_action` escape hatch (E6-03 / ADR-003). **Off by default**; set a truthy value to opt in. Enabling it does **not** widen the allowlist. |
| `INKSCAPE_MCP_ENGINE_MODE` | `per_call` | Engine transport for render/export/path/boolean/action-chain (E12 / ADR-007). `per_call` spawns a fresh Inkscape per call (default, always correct); `shell` routes those ops through one warm, long-lived `inkscape --shell` worker per document with an **automatic per-call fallback** on any fault. Enable for faster multi-op batches; any value other than `shell` floors to `per_call`. A private headless worker, **not** a channel to your live GUI. |
| `INKSCAPE_MCP_ENGINE_MAX_PROCESSES` | `2` | Max concurrent warm shell workers when `engine_mode=shell` (LRU-evicted). Floored at 1. |
| `INKSCAPE_MCP_ENGINE_IDLE_TIMEOUT_S` | `300` | Seconds before an idle warm shell worker is reaped. Floored at 1. |
| `INKSCAPE_MCP_TOOL_PROFILE` | `full` | Tool-disclosure profile (E18-03). `full` keeps the flag-allowed surface; `core` narrows `tools/list` to the curated essential authoring set (open/inspect/find/create-*/style/transform/export/snapshot) to cut per-turn model-context cost (~60% fewer tokens). Only **narrows** within the flag-allowed surface; a stray value floors to `full`. |
| `INKSCAPE_MCP_ACTION_ALLOWLIST` | *(built-in defaults)* | Inkscape Action ids **added** to the built-in allowlist (E6-02). Server-side, never client-supplied; cannot remove a default or open arbitrary passthrough; each must also be in the version-keyed capability map to run. |
| `INKSCAPE_MCP_EXTENSION_ALLOWLIST` | *(empty)* | Inkscape extension ids added to the (empty) execution allowlist (E6-02). Discovery is read-only and unaffected. |

> Truthy = `1` / `true` / `yes` / `on` (case-insensitive); everything else (incl. unset/empty) =
> the default. Numeric vars below their safety floor fall back to the default.
