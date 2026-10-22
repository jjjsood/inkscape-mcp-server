"""Argument-validation middleware: one-line, actionable errors instead of pydantic dumps.

FastMCP validates tool arguments against the tool's JSON schema BEFORE the tool body runs, so the
`guarded()` decorator never sees a malformed `targets` or `include`. Without this middleware the
client gets pydantic's multi-line report (all union branches plus an `errors.pydantic.dev` URL).
"""

from __future__ import annotations

from typing import Any

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.middleware import CallNext
from fastmcp.tools import ToolResult
from pydantic import ValidationError

from inkscape_mcp.validation_errors import format_validation_errors


class CompactValidationErrors(Middleware):
    """Turn a pydantic `ValidationError` on tool arguments into a short `ToolError`."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except ValidationError as exc:
            detail = format_validation_errors(exc.errors(include_url=False, include_input=False))
            tool: Any = context.message.name
            raise ToolError(f"invalid {tool} arguments: {detail}") from None
