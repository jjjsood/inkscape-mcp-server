"""Drift-guard test for `outputSchema` / structured-content coverage.

Every registered `@mcp.tool` must return a typed pydantic model so FastMCP emits a non-null
`outputSchema` on the wire tool — structured content the client can validate. A bare
`dict` / `str` / `list` / `Any` return (or a union whose non-model arm defeats schema derivation)
produces a NULL schema and silently drops structured-output validation. This test asserts the good
state holds for the WHOLE surface so a future tool cannot regress it.

The four inline-image tools (`render_preview` / `capture_frame` / `export_document` /
`export_object`) annotate `<Model> | ToolResult` to optionally append an image block; they pass an
explicit `output_schema` (derived from the bare model, identical to FastMCP's own derivation) so
their wire schema is non-null and matches the structured content they return.
"""

from __future__ import annotations

import asyncio

from inkscape_mcp.server import mcp, register_tools

# Register the full surface once so `mcp.list_tools()` reflects every `@mcp.tool`. Idempotent.
register_tools()


def _tools() -> list:
    return asyncio.run(mcp.list_tools())


def test_every_tool_emits_non_null_output_schema() -> None:
    """No registered tool may carry a null/absent `outputSchema` on its wire form."""
    tools = _tools()
    assert tools, "no tools registered — register_tools() did not wire the surface"

    missing = [t.name for t in tools if t.to_mcp_tool().outputSchema is None]
    assert not missing, (
        "tools lacking a non-null outputSchema (return a typed pydantic model, or pass an "
        f"explicit output_schema): {sorted(missing)}"
    )


def test_every_output_schema_is_an_object() -> None:
    """MCP requires `outputSchema` to describe an object type; assert it for every tool."""
    offenders = []
    for tool in _tools():
        schema = tool.to_mcp_tool().outputSchema
        if schema is None or schema.get("type") != "object":
            offenders.append(tool.name)
    assert not offenders, f"tools whose outputSchema is not an object type: {sorted(offenders)}"
