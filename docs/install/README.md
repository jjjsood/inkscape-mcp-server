# Install & operate — inkscape-mcp

Everything you need to install the server, wire it into an MCP host, understand what works on your
platform, and fix the common failures. This is the operator-facing companion to the server
[README](../../README.md).

- **[install.md](install.md)** — install via `uvx` / `pipx` (and from source), plus the entry point.
- **[host-configs.md](host-configs.md)** — ready-made config snippets for Claude Desktop, Claude
  Code, and a generic STDIO host. Example files live in [`examples/`](../../examples/).
- **[compatibility.md](compatibility.md)** — Inkscape version × OS × feature-availability matrix.
- **[troubleshooting.md](troubleshooting.md)** — common failures → fixes, driven by the
  `diagnose_runtime` / `list_capabilities` diagnostics.

## TL;DR

```bash
# 1. Install (from source / git — not yet on PyPI; see install.md)
uvx --from /path/to/inkscape-mcp inkscape-mcp   # one-shot run
pipx install /path/to/inkscape-mcp             # persistent install

# 2. Tell the server which directories it may touch (the only REQUIRED setting)
export INKSCAPE_MCP_WORKSPACE_ROOTS=/absolute/path/to/your/svgs

# 3. Point your MCP host at the `inkscape-mcp` console script over STDIO (see host-configs.md)
```

The server speaks **MCP over STDIO only** (no HTTP). Read / edit / validate tools work with no
Inkscape binary; render / export / path-geometry / live tools need Inkscape **1.4.x** on `PATH`.
Run `diagnose_runtime` to see exactly what your host supports.
