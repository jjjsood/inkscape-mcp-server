"""Inkscape CLI render/export engine (E1-06, ADR-005 Inkscape engine).

Render previews and export documents/objects via the Inkscape CLI. Pure functions in
`cli.py` build argument lists (never shell strings), run them through
`inkscape_mcp.workspace.subprocess_exec.run_inkscape`, write only under the per-document
artifact/exports dir, and enforce the §4 export limits. The MCP tool layer lives in
`inkscape_mcp.tools.export`.
"""
