"""FastMCP application entry point for inkscape-mcp.

Tools/Resources/Prompts are registered here. Keep this file thin: define the app,
register the ten tools from `inkscape_mcp.tools`, and run STDIO.

Mutating calls are risk-classed (HIGH needs an approval_token) and emit one snapshot and one
Operation Record per call. Transport is STDIO only.
"""

from __future__ import annotations

from fastmcp import FastMCP

from inkscape_mcp.overview import SYSTEM_OVERVIEW
from inkscape_mcp.tools._middleware import CompactValidationErrors

# Shared app instance. Resource and prompt modules import this and attach via @mcp.resource /
# @mcp.prompt; the tool modules are attached by `tools.register_all`.
#: `instructions` delivers a concise system overview IN-CONTEXT every turn (the document
# model + doc_id lifecycle, the snapshot/restore reversibility idiom, the risk classes + the
# approval_token gate, the intended tool ordering, and the render-and-look default), routing to the
# generated discovery surface (`help` / `llms.txt`) for specifics — so the
# model holds the idioms without having to open a docs/ file.
mcp: FastMCP = FastMCP("inkscape-mcp", instructions=SYSTEM_OVERVIEW, on_duplicate="replace")
mcp.add_middleware(CompactValidationErrors())


def register_tools() -> None:
    """Register the tools, resources and prompts on the shared ``mcp`` app.

    Resource and prompt modules decorate against ``mcp`` on import; the ten tools are attached by
    ``tools.register_all``. Safe to call repeatedly (registration replaces).
    """
    # Configure structured logging (stderr only — stdout is the MCP STDIO channel).
    from inkscape_mcp.logging_setup import configure_logging, get_logger

    configure_logging()

    # Snapshot retention startup sweep (workspace-model §6): an explicit, best-effort pass that
    # reclaims superseded snapshots + orphaned Operation Records left by prior sessions. A sweep
    # failure must never block boot or touch a working copy/original.
    from inkscape_mcp.retention import sweep_all_roots

    try:
        sweep_all_roots()
    except Exception:  # pragma: no cover - boot must survive a sweep error
        get_logger("retention").exception("startup snapshot sweep failed")

    # resources, then tools — self-register via @mcp.resource / @mcp.tool on import.
    # MCP Prompts (architecture §4.1). The `live_canvas_assist` Prompt orients the agent on
    # the live-view perceive→decide→act→observe loop; the export/recolor library
    # (`prepare_web_export` / `prepare_icon_set` / `prepare_print_export` / `theme_recoloring`)
    # orients it on the shipped export + recolor surface; the authoring library
    # (`compose_artwork` / `restyle_artwork`) on-ramps the generative create→draw→render→export
    # loop. Each @mcp.prompt decorator self-registers on import. Prompts add no authority — the work
    # still flows through the typed, gated tools.
    #: the `inkscape://prompts` index resource surfaces the registered prompts through the
    # RESOURCE surface (`ListMcpResourcesTool`), which cannot otherwise see prompts; imported after
    # the prompt modules so the index reads a fully-registered prompt registry.
    from inkscape_mcp.prompts import authoring as authoring_prompts  # noqa: F401
    from inkscape_mcp.prompts import library as library_prompts  # noqa: F401
    from inkscape_mcp.prompts import live as live_prompts  # noqa: F401
    from inkscape_mcp.resources import document as document_resources  # noqa: F401
    from inkscape_mcp.resources import live as live_resources  # noqa: F401
    from inkscape_mcp.resources import prompts as prompts_resources  # noqa: F401
    from inkscape_mcp.resources import runtime  # noqa: F401

    # tools, then the post-registration passes (tags, annotations, compact schemas, flag gating).
    # Registration replaces an earlier one, so tests may call `register_tools()` repeatedly; the
    # gating pass is re-evaluatable, so the visible surface always matches the current flags.
    from inkscape_mcp.tool_tags import apply_disclosure, apply_tool_meta
    from inkscape_mcp.tools import register_all

    register_all(mcp)
    apply_tool_meta(mcp)
    apply_disclosure(mcp)


def main() -> None:
    """Console-script entry point (`inkscape-mcp`). Runs the server over STDIO."""
    register_tools()
    mcp.run()  # STDIO transport by default


if __name__ == "__main__":
    main()
