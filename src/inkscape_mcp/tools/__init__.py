"""The ten result-oriented tools: `open`, `inspect`, `edit`, `render`, `export`, `history`, `help`,
`live`, `live_edit` and the advanced `actions`.

Each module exposes `register(mcp)`; `register_all` attaches all of them. Visibility is not decided
here: `tool_tags.apply_disclosure` hides `live`/`live_edit` and `actions` per the operator flags.
"""

from fastmcp import FastMCP

from inkscape_mcp.tools._middleware import CompactValidationErrors


def register_all(mcp: FastMCP) -> None:
    """Register every tool on `mcp`. Re-registering replaces the earlier registration."""
    from inkscape_mcp.tools import (
        actions,
        edit,
        export,
        help_,
        history,
        inspect_,
        live,
        live_edit,
        open_,
        render,
    )

    for module in (open_, inspect_, edit, render, export, history, help_, live, live_edit, actions):
        module.register(mcp)


def build_app() -> FastMCP:
    """A fresh FastMCP app with only the tools (no instructions, resources or prompts)."""
    app: FastMCP = FastMCP("inkscape-mcp", on_duplicate="replace")
    app.add_middleware(CompactValidationErrors())
    register_all(app)
    return app
