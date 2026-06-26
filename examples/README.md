# Example MCP host configs

Ready-to-copy launch configs for the `inkscape-mcp` STDIO server. Replace every
`/absolute/path/...` placeholder with a real **absolute** path before use — STDIO hosts run with a
different working directory than your shell, so relative paths and `~` are unreliable.

| File | Host |
|---|---|
| [`claude_desktop_config.json`](claude_desktop_config.json) | Claude Desktop (`mcpServers` stanza). |
| [`mcp.json`](mcp.json) | Claude Code project `.mcp.json` / any generic STDIO host. |

Both use the `uvx --from <path>` launch form (no persistent install). To use a `pipx install`
instead, set `"command": "inkscape-mcp"` and `"args": []`. For development/dogfooding use
`"command": "uv"`, `"args": ["run", "--directory", "/abs/.../inkscape-mcp-server", "inkscape-mcp"]`.

`INKSCAPE_MCP_WORKSPACE_ROOTS` is the only **required** env var. See
[`../../docs/install/host-configs.md`](../../docs/install/host-configs.md) for the full env-var table,
the `claude mcp add` CLI form, and the generic-host contract; and
[`../../docs/install/install.md`](../../docs/install/install.md) for install paths.
</content>
