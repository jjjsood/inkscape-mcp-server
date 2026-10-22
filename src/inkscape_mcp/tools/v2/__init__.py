"""Result-oriented tool surface (staging package).

Ten tools (`open`, `inspect`, `edit`, `render`, `export`, `history`, `help`, `live`, `live_edit`
and, in advanced mode, `actions`) over the engine modules. Nothing here is imported by
``server.py`` yet: :func:`build_app` assembles a standalone app for tests and measurements, and the
wiring phase moves these modules up into ``inkscape_mcp.tools`` and calls :func:`register_all`.
"""

from fastmcp import FastMCP


def register_all(mcp: FastMCP, *, advanced: bool = False) -> None:
    """Register the tools on `mcp`. `advanced=True` adds the `actions` escape hatch."""
    from inkscape_mcp.tools.v2 import (
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

    for module in (open_, inspect_, edit, render, export, history, help_, live, live_edit):
        module.register(mcp)
    if advanced:
        actions.register(mcp)


def build_app(*, advanced: bool = False) -> FastMCP:
    """A fresh FastMCP app carrying only the new tools (no instructions, no old surface)."""
    app: FastMCP = FastMCP("inkscape-mcp")
    register_all(app, advanced=advanced)
    return app
