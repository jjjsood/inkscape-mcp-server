"""Safe-edit engine.

Direct-DOM, reversible SVG edits (ADR-005). `dom` holds the low-level lxml primitives
(load/write working copy, target resolution, style/transform/colour helpers); `pipeline`
holds the shared mutating-edit wrapper that snapshots, mutates, and links a before/after
preview to an Operation Record. The style/text/transform/save engine modules build on
both. No MCP decorators live here — the `inkscape_mcp.tools.*` layer wraps these pure functions
and maps their exceptions to `ToolError`.
"""
