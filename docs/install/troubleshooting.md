# Troubleshooting

Most failures are environment, not bugs. The fastest diagnosis is the **`diagnose_runtime`** tool
(forces a fresh probe) or **`list_capabilities`** (cached) — both return the same
[`Capabilities`](../../src/inkscape_mcp/runtime/probe.py) shape, and the probe
**degrades gracefully**: a missing backend becomes a `false`/`null` field plus a human-readable entry
in `notes`, never a crash. Read `notes` first.

Every probe note below is the **exact string** the server emits, so you can grep for it.

## Read the diagnostics

`diagnose_runtime()` returns (key fields):

| Field | Meaning |
|---|---|
| `inkscape_available`, `inkscape_binary`, `inkscape_version`, `inkscape_version_tuple` | Was an Inkscape binary found + what version. |
| `meets_minimum` | Version ≥ 1.3.0 floor. |
| `actions`, `has_export_actions` / `has_object_actions` / `has_path_actions` / `has_select_actions` | Action surface from `--action-list`. |
| `export_types` | Tokens parsed from `--export-type=` in `--help`. |
| `shell_mode_available` | Whether the opt-in warm `inkscape --shell` engine (E12/ADR-007) can run here (true ⇔ an Inkscape binary is present). Whether it is USED is the separate `INKSCAPE_MCP_ENGINE_MODE` setting; `shell` always falls back to per-call on any fault. |
| `system_data_dir`, `user_data_dir` | Inkscape data dirs (used for inkex + live helper discovery). |
| `python_version`, `inkex_path`, `inkex_version` | Server interpreter + bundled inkex (read, never imported). |
| `dbus_session_bus`, `dbus_inkscape_present` | DBus fast-path availability. |
| `live_extension_socket_available` | Whether the live helper is installed under a data dir. |
| `font_count` | `fc-list` face count (0 ⇒ fontconfig broken/absent). |
| `notes` | Degradation messages — **start here**. |

Workspace-root problems are **not** in `Capabilities`; they surface in `Settings.root_diagnostics`
(from [`config.py`](../../src/inkscape_mcp/config.py)) and in the error a tool
raises when it has no usable root.

## Symptom → cause → fix

### Inkscape not found / render & export fail

- **Probe note:** `inkscape not found on PATH`. `inkscape_available=false`, `inkscape_binary=null`.
- **Cause:** no `inkscape` on the subprocess `PATH`. MCP hosts often launch with a minimal `PATH` that
  differs from your interactive shell.
- **Fix:** install Inkscape **1.4.x** and ensure it is on the *host-launched* process `PATH`. Verify
  with `inkscape --version`. Read / edit / validate / snapshot tools work without it; only render /
  export / path-geometry / live need the binary.

### Inkscape too old

- **Probe note:** `could not parse inkscape version string` (unparsable) or `meets_minimum=false`.
- **Cause:** version below the 1.3.0 floor (`MINIMUM_VERSION`).
- **Fix:** upgrade to 1.4.x (the target). Older versions are not hard-blocked but are unverified.

### `inkscape --version` / `--action-list` launches but fails

- **Probe notes:** `inkscape <args> failed to launch: <err>`, `inkscape <args> exited <code>`,
  `inkscape <args> timed out after <N>s`, or `inkscape --action-list returned no parseable actions`.
- **Cause:** broken install, a sandbox/permissions issue, or a slow first launch hitting the
  per-process timeout.
- **Fix:** run the command by hand to see the real error. If it is just slow, raise
  `INKSCAPE_MCP_PROCESS_TIMEOUT_S` (default `60`).

### An export format is rejected / PDF won't export

- **Probe note:** `could not parse --export-type list from inkscape --help`; check the `export_types`
  field.
- **Cause:** PDF / PS / EPS are **not** in the `--export-type` list — they are action-pipeline
  outputs verified at runtime. PNG / SVG (incl. plain) are the confirmed direct types.
- **Fix:** confirm the format is in `export_types` (or is one of the action-pipeline formats your host
  actually produces). Stick to PNG / SVG when unsure.

### Export "too large" / size-limit errors

- **Cause:** the request exceeds a configured cap: raster dimension > `INKSCAPE_MCP_MAX_EXPORT_PX`
  (`8192`), input SVG > `INKSCAPE_MCP_MAX_INPUT_BYTES` (50 MiB), or produced artifact >
  `INKSCAPE_MCP_MAX_OUTPUT_BYTES` (100 MiB) / the artifact byte budgets.
- **Fix:** lower the requested dimension, or raise the relevant env cap (see
  [host-configs.md](host-configs.md)). Caps have safety floors — a non-positive value falls back to
  the default rather than disabling the gate.

### Fonts render wrong / text shifts / `font_count` is 0

- **Probe notes:** `fc-list unavailable; font count is 0` or `font count is 0; fontconfig may be
  broken`.
- **Cause:** fontconfig is missing or misconfigured, so Inkscape substitutes fonts.
- **Fix:** install fontconfig + the fonts your documents use; verify with `fc-list | wc -l`. On
  headless Linux containers, install a fontconfig package and at least one font family.

### `inkex` not found / inkex-native features unavailable

- **Probe notes:** `inkex not found under any data dir`, `inkex __version__ not found in sources`,
  `inkex __init__.py unreadable: <err>`.
- **Cause:** Inkscape's bundled `inkex` wasn't located under the data dirs. (The server **reads**
  inkex sources for a version; it never imports inkex — importing triggers a numpy chain that fails on
  some hosts.) The MVP edit/render paths use `lxml` + the Inkscape CLI and do **not** require inkex.
- **Fix:** usually nothing to do for MVP tools. If you need inkex discovery, ensure Inkscape is
  installed so `--system-data-directory` / `--user-data-directory` resolve.

### Live mode disabled

- **Symptom:** `live_connect` refuses cleanly; `live_status` shows `enabled=false`.
- **Cause:** `INKSCAPE_MCP_LIVE_ENABLED` set to a falsy value (`0`/`false`/`no`/`off`).
- **Fix:** unset it (defaults to on) or set a truthy value. Headless is unaffected either way.

### Live mode won't connect

- **Probe field:** `live_extension_socket_available=false` (helper not installed); `dbus_session_bus`
  / `dbus_inkscape_present` for the DBus fast-path.
- **Probe notes (DBus):** `no DBUS_SESSION_BUS_ADDRESS; session bus unavailable`,
  `gdbus unavailable; cannot probe live Inkscape on session bus`, `gdbus failed to launch: <err>`,
  `gdbus list-names timed out after <N>s`, `gdbus list-names exited <code>`.
- **Cause:** no *running* Inkscape, the helper extension isn't installed (cross-platform path), or no
  DBus session bus / no Inkscape on the bus (Linux fast-path only).
- **Fix:** start Inkscape, then install the helper via the `live_install_helper` tool or
  `scripts/install-live-helper.sh` / `install-live-helper.ps1`. DBus is an *optional* Linux fast-path;
  when it's absent the server uses the extension-socket transport — that is expected, not an error.
  Use `check_live_support` to see every transport probed on your host.

### No DBus session bus (Linux)

- **Probe note:** `no DBUS_SESSION_BUS_ADDRESS; session bus unavailable`. `dbus_session_bus=false`.
- **Cause:** running outside a desktop session (SSH, container, CI).
- **Fix:** none needed for the cross-platform extension-socket transport. DBus is a fast-path only; if
  you specifically want it, run inside a session with `DBUS_SESSION_BUS_ADDRESS` exported.

### Workspace root not configured / unusable

- **Symptom:** tools refuse with a "no usable workspace root" error; `Settings.root_diagnostics` has
  entries.
- **Diagnostics (from `config.py`):** `workspace root does not exist: '<path>'`,
  `workspace root is not a directory: '<path>'`,
  `workspace root is not readable+writable: '<path>'`.
- **Cause:** `INKSCAPE_MCP_WORKSPACE_ROOTS` unset, or every configured root is missing / not a
  directory / not read+writable. The server **never** auto-creates a root and **never** falls back to
  CWD or home.
- **Fix:** set `INKSCAPE_MCP_WORKSPACE_ROOTS` to one or more existing, writable, **absolute**
  directories (OS-path-separator-delimited: `:` on Linux/macOS, `;` on Windows). Create the directory
  yourself first.

### Server starts but the host shows no tools / hangs

- **Cause:** the launched command never reaches `inkscape_mcp.server:main`, or something is writing to
  **stdout** (which is the MCP channel) — wrappers and shells must not print to stdout.
- **Fix:** confirm the command boots standalone: `inkscape-mcp </dev/null` (or `uv run inkscape-mcp`).
  The server logs to **stderr only**. Re-check the host `command`/`args` against
  [host-configs.md](host-configs.md), and use **absolute** paths (STDIO hosts don't inherit your
  shell's working dir).

### `uvx` / `pipx` can't find `inkscape-mcp`

- **Cause:** the package is **not on PyPI**, so the bare name won't resolve.
- **Fix:** use the from-source / from-git forms in [install.md](install.md), e.g.
  `uvx --from /abs/.../inkscape-mcp inkscape-mcp` or
  `pipx install /abs/.../inkscape-mcp`.

## Still stuck

Re-run `diagnose_runtime` and read `notes` end to end. Cross-reference
[compatibility.md](compatibility.md) for what your platform is expected to support.
