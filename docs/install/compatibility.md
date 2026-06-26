# Compatibility matrix — Inkscape version × OS × feature

What works where. The server **detects at runtime and never assumes a version** — run
`diagnose_runtime` / `list_capabilities` to get the ground truth for *your* host. This page is the
operator-facing summary; `diagnose_runtime` is canonical for the exact probe result and the
per-action breakdown on your machine.

**Verified vs expected.** ✅ = verified on the development host (Inkscape **1.4.3** on **Linux**) or in
CI. 🟡 = expected to work but **not yet verified with a real Inkscape** on that platform. ⛔ = not
supported.

**CI coverage (E7-02).** [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) runs the
headless feature suite + the cross-platform live-transport suite + the packaged `pipx` install on
**Linux, macOS, and Windows**, and the full suite incl. real-Inkscape (`@pytest.mark.inkscape`)
tests on **Linux** (apt). So macOS/Windows are proven for everything *except* the real-binary
render/export/path-geometry path, which stays 🟡 there until a macOS/Windows Inkscape install is
added to CI.

## Inkscape version

| Inkscape | Headless (read/edit/validate) | Render / export / path geometry | Live — extension-socket | Live — DBus fast-path |
|---|---|---|---|---|
| none on PATH | ✅ (read/edit/validate need no binary) | ⛔ (needs binary) | ⛔ | ⛔ |
| < 1.3 | ✅ | 🟡 (below the 1.3.0 minimum floor; `meets_minimum=false`, use at own risk) | 🟡 | ⛔ (`org.gtk.Actions` export is 1.2+) |
| 1.3.x | ✅ | 🟡 | 🟡 | 🟡 (Linux) |
| **1.4.x (target)** | ✅ | ✅ | 🟡 (helper not yet shipped/installed by default) | ✅ (Linux) |

Minimum version floor is **1.3.0** (`MINIMUM_VERSION` in
[`runtime/probe.py`](../../src/inkscape_mcp/runtime/probe.py)); below it
`meets_minimum` is `false` but tools are not hard-blocked.

## Operating system

| Feature | Linux | macOS | Windows |
|---|---|---|---|
| Headless: read / inspect / validate (no Inkscape needed) | ✅ | 🟡 | 🟡 |
| Headless: render / export / path geometry (Inkscape CLI) | ✅ | 🟡 | 🟡 |
| Live read/write — **extension-socket** transport (primary, all OS — **modal: freezes the GUI while serving**) | ✅ (path tested) | 🟡 | 🟡 |
| Live — **DBus** transport (`org.gtk.Actions`, optional fast-path — **no GUI freeze**) | ✅ (session bus present here) | ⛔ | ⛔ |
| Snapshots / Operation Records / sandbox | ✅ | 🟡 | 🟡 |

The live layer is **never tied to one OS**: the extension-socket bridge (helper `inkex` extension on
a loopback socket) is the cross-platform primary; DBus is a Linux/BSD-only fast-path layered on top
when a session bus + a live `org.inkscape.Inkscape*` instance are present. `--app-id-tag` is **not** a
control API.

**No-freeze caveat (2026-06-14).** The extension-socket bridge runs as a *modal* `inkex` effect
extension, so it **freezes the Inkscape GUI for the whole live session**. Only the Linux DBus
transport (`org.gtk.Actions.Activate`, runs in Inkscape's own main loop) is no-freeze — see E3-07.
Windows/macOS live is therefore modal/best-effort. `inkscape --actions=…` does **not** forward to a
running instance (`Gio::APPLICATION_NON_UNIQUE`), so it is not a live channel. Full per-OS matrix:
architecture doc §4.4 "Concurrency model — the no-freeze reality".

## Feature availability rules

- **Headless is always available** and never depends on live mode or the DBus session bus. Read /
  edit / validate work with no Inkscape binary at all; render / export / path geometry require
  Inkscape on `PATH`.
- **Live mode** (E3 read / E4 write) is gated by `INKSCAPE_MCP_LIVE_ENABLED` (on by default). It
  needs a *running* Inkscape and, for the cross-platform path, the helper extension installed
  (`live_install_helper`, or the `scripts/install-live-helper.{sh,ps1}` installers).
- **DBus live** additionally needs `DBUS_SESSION_BUS_ADDRESS` set and an Inkscape instance on the
  session bus (probed with `gdbus list-names --session`). Absent ⇒ the server falls back to the
  extension-socket transport, never errors.
- **Export formats are per-host.** PNG / SVG (incl. plain SVG) are confirmed; PDF / PS / EPS are
  action-pipeline outputs that the probe verifies at runtime (they are **not** in the
  `--export-type` list). Check `export_types` from `diagnose_runtime`.

## How to confirm on your host

```bash
# Via the MCP tool (preferred — exact same probe the server uses):
#   diagnose_runtime()  -> Capabilities { inkscape_version, meets_minimum, export_types,
#                                         dbus_session_bus, live_extension_socket_available, ... }

# Or directly:
inkscape --version
inkscape --action-list | wc -l
inkscape --help | grep -- --export-type
echo "$DBUS_SESSION_BUS_ADDRESS"          # non-empty => session bus present (DBus fast-path possible)
```

Or just call the `diagnose_runtime` tool. Map any gap to a fix in
[troubleshooting.md](troubleshooting.md).
