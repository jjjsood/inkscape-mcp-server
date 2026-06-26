"""Runtime capability probing (E1-03).

Pure probe engine (`probe.py`) plus the machine-readable `Capabilities` model. No MCP
decorators live here — the `inkscape_mcp.tools.system` and `inkscape_mcp.resources.runtime`
modules wrap these probes for the wire. Never assume capabilities; detect at runtime and
degrade gracefully when a backend is absent.
"""
