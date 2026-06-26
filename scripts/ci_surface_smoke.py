#!/usr/bin/env python3
"""Full-surface MCP primitive smoke for ``inkscape-mcp`` (E9-03).

Enumerates the LIVE server over the FastMCP in-memory client and asserts the WHOLE primitive
surface is both *registered* and *reachable*. It is designed to catch the two failure modes that
slipped past the boot smoke on 2026-06-16:

1. A **stale process** exposing the wrong surface (e.g. 61 tools / 0 prompts). The count
   assertions below fail loudly on ANY drift, printing an expected-vs-actual diff naming exactly
   which primitives appeared or disappeared.
2. A **resource serialization bug** (the E9-01 fix) that lets a resource register but blow up on
   read. Every resource is actually READ here — static URIs and the templated
   ``inkscape://document/{doc_id}/*`` set against a real fixture doc — and each payload must parse
   as JSON.

It also calls every read-only / dry-run tool once to prove they are reachable without error and
without mutating an original or needing an approval token.

The expected counts live in ONE place (the constants below). A future epic that adds or removes a
tool / prompt / resource updates these numbers deliberately, and the diff makes the delta obvious.

Headless-safe: this must pass on a CI runner with NO Inkscape binary. Any tool or call that needs
the Inkscape CLI is guarded behind ``shutil.which("inkscape")`` and skipped gracefully (a
``skip: ...`` line) rather than failing. The count assertions, the resource reads, and the
pure-DOM / read-only tool calls all pass with or without Inkscape.

Usage (cross-platform, run inside ``inkscape-mcp-server/``):

    uv run python scripts/ci_surface_smoke.py

Exit 0 only when the counts match, every resource read + parsed as JSON, and every attempted tool
call succeeded. Any drift / read failure / unexpected tool error exits non-zero with a legible
message on stderr.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

# --- Expected surface (THE single source of truth — update deliberately per epic) ------------
#: Confirmed surface as of E14 (E9-03's 64/5/13 + E11's `fit_to_content`/`tile` tools, the
#: `inkscape://documents` resource, E14-01/E14-04's 14 creation/defs/grouping tools:
#: create_rect/circle/ellipse/line/polygon/polyline/path/text, add_linear_gradient,
#: add_radial_gradient, create_group, group_objects, reparent_object, create_use; and E14-02/03/06's
#: 4 document tools: create_document, reload_document, set_document_svg, insert_svg_fragment). E12
#: (headless shell engine) adds NO new MCP surface. E14-07 adds 1 read-only discovery tool
#: (find_objects). E14-08 adds 1 read-only discoverability tool (how_do_i) and an additive `intents`
#: section on the capabilities matrix (no new tool beyond how_do_i). E16 adds its read/verify and
#: collection tools (stat_artifact[s], compose_grid + the *_set tools, delete_object, …) and E16-10
#: adds 2 more (place_document, live_arm_socket) → 97. E19-01 adds the `apply_edits` typed batch
#: tool → 98. E20-02 adds the `transform_objects` selector→op tool → 99. A future epic that changes
#: the tool/prompt/resource set bumps the matching constant; the diff pinpoints what moved.
EXPECTED_TOOLS = 99
#: 5 prior prompts (E5-07 export/recolor x4 + E8-05 live_canvas_assist) + E15-04's 2 authoring
#: prompts (compose_artwork, restyle_artwork) = 7. No new @mcp.tool — tool count is unchanged.
EXPECTED_PROMPTS = 7
#: Resources are static + 7 document templates; MCP reports static + templated separately, so we
#: assert their SUM (a template registered as a plain resource, or vice-versa, still has to add up).
#: E14-08b adds the static `inkscape://runtime/intents` resource; E16-10e adds the static
#: `inkscape://prompts` index resource → 9 static + 7 templates = 16.
EXPECTED_RESOURCES = 16

#: The 8 static resource URIs (no ``{placeholder}``) — read directly.
STATIC_RESOURCE_URIS = (
    "inkscape://runtime/capabilities",
    "inkscape://runtime/intents",
    "inkscape://live/session",
    "inkscape://live/selection",
    "inkscape://live/view",
    "inkscape://live/events",
    "inkscape://live/operations",
    "inkscape://documents",
)

#: The 7 templated document resource suffixes — read with a real ``doc_id`` from a fixture doc.
DOCUMENT_RESOURCE_SUFFIXES = (
    "summary",
    "tree",
    "layers",
    "objects",
    "styles",
    "fonts",
    "assets",
)

#: Minimal, valid SVG fixture: a viewBox + one identified rect. Enough for inspect / validate /
#: quality / optimize / batch-export-dry-run to exercise real code paths.
_FIXTURE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" '
    'width="100" height="100">'
    '<rect id="r1" x="10" y="10" width="40" height="40" fill="#3366cc"/>'
    '<rect id="r2" x="50" y="50" width="40" height="40" fill="#cc3366"/>'
    "</svg>"
)

#: True iff the Inkscape CLI is on PATH. Gates every tool/op that shells out to the engine.
_HAS_INKSCAPE = shutil.which("inkscape") is not None


def _names(items: list[Any], attr: str = "name") -> set[str]:
    """Pull the comparable identifier off each enumerated primitive (name or URI/template)."""
    out: set[str] = set()
    for it in items:
        val = getattr(it, attr, None)
        if val is None:
            val = getattr(it, "uri", None) or getattr(it, "uriTemplate", None)
        out.add(str(val))
    return out


def _diff(label: str, expected: int, actual: int, names: set[str]) -> list[str]:
    """Build a loud, legible drift report for one primitive class (only when it drifted)."""
    if expected == actual:
        return []
    lines = [
        f"DRIFT [{label}]: expected {expected}, got {actual} (delta {actual - expected:+d})",
        f"  current {label} ({actual}):",
    ]
    lines += [f"    - {n}" for n in sorted(names)]
    lines.append(
        f"  -> if this change is intentional, update EXPECTED_{label.upper()} in "
        "scripts/ci_surface_smoke.py (the single source of truth)."
    )
    return lines


def _resource_text(read_result: Any) -> str:
    """Extract the text payload from a ``read_resource`` result (list of content blocks)."""
    items = read_result if isinstance(read_result, list) else [read_result]
    for item in items:
        text = getattr(item, "text", None)
        if text is not None:
            return str(text)
    raise AssertionError("resource read returned no text content block")


def _result_dict(call_result: Any) -> dict[str, Any]:
    """Get a tool call result as a plain dict (prefer the structured content channel)."""
    structured = getattr(call_result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    data = getattr(call_result, "data", None)
    if isinstance(data, dict):
        return data
    if data is not None and hasattr(data, "model_dump"):
        return dict(data.model_dump())
    return {}


async def _assert_counts(client: Any) -> int:
    """Enumerate the surface and assert exact counts. Returns the number of failures (0 = ok)."""
    tools = await client.list_tools()
    resources = await client.list_resources()
    templates = await client.list_resource_templates()
    prompts = await client.list_prompts()

    n_tools = len(tools)
    n_prompts = len(prompts)
    n_resources_total = len(resources) + len(templates)

    print(
        f"surface: tools={n_tools} prompts={n_prompts} "
        f"resources={len(resources)}+templates={len(templates)}={n_resources_total}",
        flush=True,
    )

    report: list[str] = []
    report += _diff("tools", EXPECTED_TOOLS, n_tools, _names(tools))
    report += _diff("prompts", EXPECTED_PROMPTS, n_prompts, _names(prompts))
    report += _diff(
        "resources",
        EXPECTED_RESOURCES,
        n_resources_total,
        _names(resources, "uri") | _names(templates, "uriTemplate"),
    )

    if report:
        print(
            f"\nSURFACE DRIFT DETECTED "
            f"(expected {EXPECTED_TOOLS} tools / {EXPECTED_PROMPTS} prompts / "
            f"{EXPECTED_RESOURCES} resources):",
            file=sys.stderr,
            flush=True,
        )
        for line in report:
            print(line, file=sys.stderr, flush=True)
        return 1

    print(
        f"counts OK: {EXPECTED_TOOLS} tools / {EXPECTED_PROMPTS} prompts / "
        f"{EXPECTED_RESOURCES} resources",
        flush=True,
    )
    return 0


async def _read_all_resources(client: Any, doc_id: str) -> int:
    """Read every resource (static + templated) and require each payload to parse as JSON.

    Returns the number of failures (0 = every resource served valid JSON).
    """
    failures = 0
    uris = list(STATIC_RESOURCE_URIS)
    uris += [f"inkscape://document/{doc_id}/{suffix}" for suffix in DOCUMENT_RESOURCE_SUFFIXES]

    for uri in uris:
        try:
            text = _resource_text(await client.read_resource(uri))
            json.loads(text)  # serialization smoke: must be valid JSON (catches the E9-01 bug)
        except Exception as exc:
            failures += 1
            print(f"resource READ FAILED: {uri}: {exc!r}", file=sys.stderr, flush=True)
        else:
            print(f"resource OK: {uri}", flush=True)

    return failures


async def _call_readonly_tools(client: Any, doc_id: str) -> int:
    """Call every read-only / dry-run tool once. Returns the number of failures (0 = all ok).

    Each entry is ``(name, args, needs_inkscape)``. Inkscape-gated calls are skipped (not failed)
    when no binary is on PATH so the smoke stays green on a headless runner.
    """
    calls: list[tuple[str, dict[str, Any], bool]] = [
        # Diagnostics (pure-Python probe; degrades gracefully with no Inkscape).
        ("diagnose_runtime", {}, False),
        ("list_capabilities", {}, False),
        ("list_actions", {}, False),
        # Intent → tool discoverability (E14-08): read-only guidance, no engine, no mutation.
        ("how_do_i", {"goal": "draw a rectangle"}, False),
        # Read-only document inspection / validation / quality (direct DOM, no engine).
        ("inspect_document", {"doc_id": doc_id}, False),
        ("validate_document", {"doc_id": doc_id}, False),
        ("quality_report", {"doc_id": doc_id}, False),
        # Dry-run path ops (HIGH-risk tools, but dry_run validates only — no engine, no mutation).
        ("simplify_path", {"doc_id": doc_id, "object_ids": ["r1"], "dry_run": True}, False),
        (
            "boolean_union",
            {"doc_id": doc_id, "object_ids": ["r1", "r2"], "dry_run": True},
            False,
        ),
        # Dry-run batch export: validates specs + projects sizes, writes nothing (no engine).
        (
            "export_batch",
            {"doc_id": doc_id, "specs": [{"format": "png", "width_px": 32}], "dry_run": True},
            False,
        ),
        # Web optimize: pure-DOM, reversible; lands only on the throwaway working copy (no engine,
        # original fixture untouched).
        ("svg_web_optimize", {"doc_id": doc_id}, False),
        # Action-chain validation needs the version-keyed capability map (probes Inkscape), so a
        # successful (allowlisted + present) chain requires the binary. Guarded.
        (
            "validate_action_chain",
            {"steps": [{"action": "select-by-id", "args": ["r1"]}]},
            True,
        ),
    ]

    failures = 0
    for name, args, needs_inkscape in calls:
        if needs_inkscape and not _HAS_INKSCAPE:
            print(f"skip: {name} (no inkscape on PATH)", flush=True)
            continue
        try:
            result = await client.call_tool(name, args)
        except Exception as exc:
            failures += 1
            print(f"tool CALL FAILED: {name}: {exc!r}", file=sys.stderr, flush=True)
            continue
        # A structured result is enough; surface a one-line marker proving it was reachable.
        _result_dict(result)
        print(f"tool OK: {name}", flush=True)

    return failures


async def _run() -> int:
    """Drive the in-memory client through counts -> resource reads -> read-only tool calls."""
    # Point the workspace at a throwaway dir BEFORE the server reads settings, so opening the
    # fixture (and any working-copy mutation) is fully sandboxed and discarded with the temp dir.
    with tempfile.TemporaryDirectory(prefix="ci_surface_smoke_") as tmp:
        import os

        os.environ["INKSCAPE_MCP_WORKSPACE_ROOTS"] = tmp
        fixture = Path(tmp) / "fixture.svg"
        fixture.write_text(_FIXTURE_SVG, encoding="utf-8")

        # Import + register AFTER the workspace root is set (config is read at import/use time).
        from fastmcp import Client

        from inkscape_mcp.server import mcp, register_tools

        register_tools()

        failures = 0
        async with Client(mcp) as client:
            failures += await _assert_counts(client)

            # Open the fixture so the templated document/* resources have a real doc_id to bind.
            open_result = await client.call_tool("open_document", {"path": str(fixture)})
            doc_id = _result_dict(open_result).get("doc_id")
            if not doc_id:
                print("FATAL: open_document returned no doc_id", file=sys.stderr, flush=True)
                return 1
            print(f"fixture opened: doc_id={doc_id}", flush=True)

            failures += await _read_all_resources(client, doc_id)
            failures += await _call_readonly_tools(client, doc_id)

        return 1 if failures else 0


def main(argv: list[str]) -> int:
    if not _HAS_INKSCAPE:
        print("note: no inkscape on PATH — engine-gated calls will be skipped", flush=True)
    rc = asyncio.run(_run())
    if rc == 0:
        print("surface smoke: PASS", flush=True)
    else:
        print("surface smoke: FAIL", file=sys.stderr, flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
