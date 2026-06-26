"""FastMCP application entry point for inkscape-mcp.

Tools/Resources/Prompts are registered here. Keep this file thin: define the app,
wire up tool modules from `inkscape_mcp.tools.*`, and run STDIO.

API rules (see CONTRIBUTING.md): small typed tools, no portmanteau, risk-classed,
mutating ops emit Operation Records. Transport is STDIO only.
"""

from __future__ import annotations

from fastmcp import FastMCP

from inkscape_mcp.overview import SYSTEM_OVERVIEW

# Shared app instance. Tool modules import this and attach via @mcp.tool / @mcp.resource.
#: `instructions` delivers a concise system overview IN-CONTEXT every turn (the document
# model + doc_id lifecycle, the snapshot/restore reversibility idiom, the risk classes + the
# approval_token gate, the intended tool ordering, and the render-and-look default), routing to the
# generated discovery surface (`how_do_i` / `list_capabilities` / `llms.txt`) for specifics — so the
# model holds the idioms without having to open a docs/ file.
mcp: FastMCP = FastMCP("inkscape-mcp", instructions=SYSTEM_OVERVIEW)


def register_tools() -> None:
    """Import-and-register tool/resource modules.

    Each module under ``inkscape_mcp.tools.*`` / ``inkscape_mcp.resources.*`` decorates
    functions with ``@mcp.tool`` / ``@mcp.resource`` against the shared ``mcp`` app; the
    import side effect runs the decorators and registers them. Importing here is what wires
    the full surface onto the server at boot.
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

    #: post-registration pass — stamp every registered tool with its MCP `ToolAnnotations`
    # (`readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint` / `title`) derived
    # from ONE central map keyed off the tool's parsed risk class. Runs LAST so the full tool
    # surface is registered before annotations are applied; a future tool with a `Risk class:`
    # docstring line auto-annotates with no edit to the map.
    from inkscape_mcp.tool_annotations import apply_annotations

    #: final post-registration pass — when `INKSCAPE_MCP_TOOL_DESC=short`, replace each
    # tool's wire description with a DERIVED short form (summary + risk line) to cut the per-turn
    # `tools/list` token cost. A no-op under the default `full`. Runs LAST so the earlier risk-class
    # parse (annotations/tags) still reads the complete docstring. `gen_llms_txt` forces `full`.
    from inkscape_mcp.tool_descriptions import apply_short_descriptions

    #: after annotations, a second central pass stamps every tool with its {domain, risk}
    # tags and drives PROGRESSIVE DISCLOSURE from the existing config flags by EXCLUDING tagged
    # tools from `tools/list` (live-off hides `live`; advanced-off hides the ADR-003 hatch group:
    # `run_raw_action` + `paths` + `actions`). Gating may only NARROW the surface, never widen it;
    # with both flags ON the full surface returns. Runs LAST + is re-evaluatable so the visible
    # surface always matches the current flags.
    from inkscape_mcp.tool_tags import apply_tags_and_disclosure

    # tools plus the safe-edit tools (style / text+object / transform), save-as, and export
    # profiles, the live-mode tools (`live`, gate on by default), the HIGH-risk path
    # geometry tools (`paths`, dry-run + approval-gated), and the controlled Action surface
    # (`actions`: discovery + allowlist + versioned map + validated chains, exec HIGH + approval-
    # gated, plus the ADR-003 `run_raw_action` escape hatch — advanced-mode/OFF-by-default,
    # single allowlisted Action, dry-run default, HIGH + approval-gated) — each module's @mcp.tool
    # decorators register against the shared app on import.
    # The convenience/polish surface (`svg_web_optimize` — reversible web optimization;
    # `quality_report` — read-only machine-readable quality metrics; `export_batch` —
    # bounded, dry-run-default typed batch export) rides the same pipelines, no new authority.
    # adds the read-only `discover.how_do_i` intent→tool discoverability tool (guidance
    # only, ADR-003: no raw-action hatch) which shares the curated intent map with the `intents`
    # section of list_capabilities/diagnose_runtime.
    # adds the HIGH-risk, reversible `delete_object` in the `dom` structural-edit module
    # (snapshot + Operation Record per op via the shared pipeline; approval-gated).
    # adds the `batch.apply_edits` tool: N typed DOM edits (a discriminated union over the
    # existing ops) applied through the SAME edit kernel as one validated-first, atomic, reversible
    # operation (one snapshot + one Operation Record; risk = max over members, approval-gated when a
    # member is high). No new authority — it composes existing typed edits (ADR-002/003/004).
    # adds the `transform_objects` tool: a declarative SELECTOR (the find_objects predicate
    # engine, verbatim) → ONE typed op (from the apply_edits member set, minus its target ids)
    # fanned across every match, fed through the SAME atomic batch kernel (one snapshot + one Op
    # Record; dry-run default; max_matches bound; risk = the op's class, approval-gated when high).
    # No code escape hatch, no second matcher, no forked kernel (ADR-002/003/004).
    from inkscape_mcp.tools import (  # noqa: F401
        actions,
        batch,
        compose,
        create,
        discover,
        document,
        dom,
        export,
        export_batch,
        find,
        live,
        optimize,
        paths,
        profiles,
        quality,
        save,
        snapshots,
        style,
        system,
        text_object,
        transform,
        transform_objects,
        validate,
    )

    apply_annotations(mcp)
    apply_tags_and_disclosure(mcp)
    apply_short_descriptions(mcp)


def main() -> None:
    """Console-script entry point (`inkscape-mcp`). Runs the server over STDIO."""
    register_tools()
    mcp.run()  # STDIO transport by default


if __name__ == "__main__":
    main()
