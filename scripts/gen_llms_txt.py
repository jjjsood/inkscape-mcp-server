#!/usr/bin/env python3
"""Generate ``llms.txt`` + ``llms-full.txt`` from the LIVE MCP registry.

These two files are the agent-facing manifest of the server surface. Crucially they are
GENERATED, not hand-maintained: the survey's reference implementation (sandraschi) keeps theirs by
hand, which drifts the moment a tool is added or its description changes. Here we introspect the
real, registered surface (``register_tools()`` → ``mcp.list_tools()`` / ``list_prompts()`` /
``list_resources()`` / ``list_resource_templates()``), so the files can never describe a tool that
does not exist or miss one that does. The drift-guard test (``tests/test_llms_txt.py``) regenerates
to a temp dir and fails if the committed copies are stale.

What it writes (at the SERVER ROOT — the parent of this ``scripts/`` dir):

* ``llms.txt`` — a concise machine-readable INDEX: project one-liner, run/transport, env config
  (pulled from ``inkscape_mcp.config`` ENV_* constants), then every tool grouped by category with
  its ONE-LINE purpose (first docstring line) + risk class, then the edit ops by group.
* ``llms-full.txt`` — the FULL manifest: per tool the name, full description, risk class, and key
  params (param names + JSON-schema types from the tool's input schema); then the full op
  reference (per op: risk, fields, a validated example — the same data as ``help(op=...)`` and the
  ``inkscape://ops`` resource, from ``inkscape_mcp.edit.op_docs``); plus prompts and resources
  sections; plus the same env/run config.

A tool's risk class is DERIVED, never hardcoded here: it is the ceiling ``risk_for(tool)`` reports
(``inkscape_mcp.runtime.capabilities``). Categories come from the tool name via ``_TOOL_GROUPS``.

Run inside ``inkscape-mcp-server/``::

    uv run python scripts/gen_llms_txt.py

DO NOT edit the generated ``llms.txt`` / ``llms-full.txt`` by hand — re-run this script instead.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Make ``inkscape_mcp`` importable when run as a bare script (``python scripts/...``).
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from inkscape_mcp import config  # noqa: E402
from inkscape_mcp.runtime.capabilities import one_line_purpose  # noqa: E402

#: Server project root (parent of ``scripts/``) — where the two generated files land.
SERVER_ROOT = Path(__file__).resolve().parent.parent

#: One-liner describing the project (kept in sync with pyproject's description).
PROJECT_ONE_LINER = (
    "inkscape-mcp — an MCP server that makes Inkscape/SVG documents agent-ready: "
    "inspect, edit safely (reversible), validate, render, and export."
)

#: Tool category order + display label, keyed by tool name. Order here is the order groups appear
#: in both files; a tool missing from the map lands in `Other` (the count guard still sees it).
_TOOL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Author", ("open", "edit")),
    ("Inspect", ("inspect", "render")),
    ("Export", ("export",)),
    ("History", ("history",)),
    ("Live", ("live", "live_edit")),
    ("Advanced", ("actions",)),
    ("Help", ("help",)),
    ("Other", ()),
)

#: Reverse lookup: tool name -> group label. Built once from `_TOOL_GROUPS`.
_NAME_TO_GROUP: dict[str, str] = {name: label for label, names in _TOOL_GROUPS for name in names}

#: The canonical risk vocabulary (architecture risk classes). The manifest reports only these.
_RISK_VOCAB = ("low", "medium", "high", "restricted")

#: A header stamped at the top of every generated file so a human knows not to hand-edit it.
_GENERATED_HEADER = (
    "# GENERATED FILE — do not edit by hand.\n"
    "# Regenerate with: uv run python scripts/gen_llms_txt.py\n"
    "# Source of truth: the live MCP registry (register_tools() -> mcp.list_tools()/...).\n"
    "# Unlike a hand-maintained llms.txt, this is regenerated from the registry so it can never\n"
    "# drift from the actual tool/prompt/resource surface.\n"
)


def _group_for(tool: Any) -> str:
    """Map a tool to its category label by name (`Other` when unlisted)."""
    return _NAME_TO_GROUP.get(tool.name, "Other")


def _risk_class(tool: Any) -> str:
    """Worst-case risk class of a tool (`runtime.capabilities.risk_for` ceiling)."""
    from inkscape_mcp.runtime.capabilities import risk_for

    return risk_for(tool.name)


def _key_params(tool: Any) -> list[tuple[str, str]]:
    """Return ``(name, json_type)`` pairs for the tool's input-schema properties.

    The type is the JSON-schema ``type`` (or a joined ``anyOf``/``$ref`` hint) so the manifest shows
    each param's shape without dragging in the full schema. Order follows the schema property order.
    """
    schema = tool.parameters or {}
    props = schema.get("properties", {})
    out: list[tuple[str, str]] = []
    for name, spec in props.items():
        out.append((name, _schema_type(spec)))
    return out


def _schema_type(spec: dict[str, Any]) -> str:
    """Best-effort human-readable type for a single JSON-schema property spec."""
    if "type" in spec:
        return str(spec["type"])
    if "anyOf" in spec:
        parts = [_schema_type(s) for s in spec["anyOf"]]
        return " | ".join(dict.fromkeys(parts))  # de-dupe, preserve order
    if "$ref" in spec:
        return str(spec["$ref"]).rsplit("/", 1)[-1]
    if "enum" in spec:
        return "enum"
    return "any"


def _env_config_lines() -> list[str]:
    """Human-readable env/run config block, sourced from ``inkscape_mcp.config`` ENV_* constants.

    We surface the workspace-roots var (the sandbox boundary) and the operator-tunable limits/gates
    by enumerating the module's ``ENV_*`` names so this can never drift from the real config keys.
    """
    lines: list[str] = []
    lines.append(
        f"Workspace roots (required): {config.ENV_WORKSPACE_ROOTS} "
        "(OS-path-separator-delimited list of allowed dirs; the sandbox boundary)."
    )
    lines.append("Operator-tunable limits / gates (all optional, env-overridable):")
    for name in sorted(_env_constant_names()):
        if name == "ENV_WORKSPACE_ROOTS":
            continue
        lines.append(f"  - {getattr(config, name)}")
    return lines


def _env_constant_names() -> list[str]:
    """All ``ENV_*`` string constants exported by ``inkscape_mcp.config``."""
    return [n for n in dir(config) if n.startswith("ENV_") and isinstance(getattr(config, n), str)]


def _run_config_lines() -> list[str]:
    """The run/transport block shared by both files."""
    return [
        "Transport: STDIO only (local). No HTTP.",
        "Run: uv run inkscape-mcp",
        "Console script: inkscape-mcp = inkscape_mcp.server:main",
    ]


async def _collect() -> dict[str, Any]:
    """Register the surface and return tools / prompts / resources / templates.

    The manifest documents the FULL tool catalog, so it is generated with both progressive-
    disclosure flags (`live_enabled` / `raw_action_enabled`) forced ON — the committed
    `llms.txt` / `llms-full.txt` always describe the complete surface, independent of the operator
    env or the runtime gating that NARROWS what a default client sees.
    """
    import os

    from inkscape_mcp.config import (
        ENV_LIVE_ENABLED,
        ENV_RAW_ACTION_ENABLED,
        get_settings,
    )
    from inkscape_mcp.server import mcp, register_tools

    os.environ[ENV_LIVE_ENABLED] = "1"
    os.environ[ENV_RAW_ACTION_ENABLED] = "1"
    get_settings.cache_clear()
    register_tools()
    return {
        "tools": await mcp.list_tools(),
        "prompts": await mcp.list_prompts(),
        "resources": await mcp.list_resources(),
        "templates": await mcp.list_resource_templates(),
    }


def _grouped_tools(tools: list[Any]) -> list[tuple[str, list[Any]]]:
    """Bucket tools into the ``_TOOL_GROUPS`` order; sort each bucket by tool name."""
    buckets: dict[str, list[Any]] = {label: [] for label, _ in _TOOL_GROUPS}
    for tool in tools:
        buckets.setdefault(_group_for(tool), []).append(tool)
    out: list[tuple[str, list[Any]]] = []
    for label, _ in _TOOL_GROUPS:
        bucket = sorted(buckets.get(label, []), key=lambda t: t.name)
        if bucket:
            out.append((label, bucket))
    return out


def _op_reference() -> dict[str, Any]:
    """The full op reference (groups, per-op fields/risk/example) from the op union itself."""
    from inkscape_mcp.edit.op_docs import ops_reference

    return ops_reference()


def _ops_index_lines(ref: dict[str, Any]) -> list[str]:
    """The compact op listing for ``llms.txt``: one line per group, HIGH-risk ops starred."""
    lines = [
        "## Edit ops (the `ops` list of `edit` and `open`; `help(op=NAME)` has fields + example)",
        "",
        f"{ref['targets']}.",
        "Starred ops are HIGH risk (need approval_token).",
        "",
    ]
    for group, ops in ref["groups"].items():
        names = ", ".join(
            f"{name}{'*' if ref['ops'][name]['risk'] == 'high' else ''}" for name in ops
        )
        lines.append(f"- {group}: {names}")
    lines.append("")
    return lines


def _ops_full_lines(ref: dict[str, Any]) -> list[str]:
    """The per-op reference for ``llms-full.txt``: risk, fields and a validated example."""
    lines = ["## Edit ops", "", f"{ref['targets']}.", f"{ref['more']}."]
    for group, ops in ref["groups"].items():
        lines += ["", f"### {group}"]
        for name in ops:
            op = ref["ops"][name]
            flags = [f"Risk: {op['risk']}"]
            if op.get("needs_approval"):
                flags.append("needs approval_token")
            if op.get("creates_id"):
                flags.append("creates an id")
            fields = ", ".join(f"{n}: {t}" for n, t in op["fields"].items())
            lines += [
                "",
                f"#### {name}",
                " · ".join(flags),
                f"Does: {op['does']}",
                f"Fields: {fields}",
                f"Example: {json.dumps(op['example'], separators=(',', ':'), ensure_ascii=False)}",
            ]
    lines.append("")
    return lines


def render_index(surface: dict[str, Any]) -> str:
    """Build the concise ``llms.txt`` INDEX content."""
    tools: list[Any] = surface["tools"]
    lines: list[str] = [_GENERATED_HEADER, "", "# inkscape-mcp — LLM index (llms.txt)", ""]
    lines.append(PROJECT_ONE_LINER)
    lines.append("")
    lines.append(
        f"Surface: {len(tools)} tools, {len(surface['prompts'])} prompts, "
        f"{len(surface['resources']) + len(surface['templates'])} resources."
    )
    lines.append("")

    lines.append("## Run & transport")
    lines += _run_config_lines()
    lines.append("")

    lines.append("## Environment / configuration")
    lines += _env_config_lines()
    lines.append("")

    lines.append("## Tools (one line + risk class, grouped by category)")
    for label, bucket in _grouped_tools(tools):
        lines.append("")
        lines.append(f"### {label}")
        for tool in bucket:
            purpose = one_line_purpose(tool.description)
            risk = _risk_class(tool)
            lines.append(f"- {tool.name} [risk: {risk}] — {purpose}")
    lines.append("")

    lines += _ops_index_lines(_op_reference())
    return "\n".join(lines)


def render_full(surface: dict[str, Any]) -> str:
    """Build the full ``llms-full.txt`` manifest content."""
    tools: list[Any] = surface["tools"]
    prompts: list[Any] = surface["prompts"]
    resources: list[Any] = surface["resources"]
    templates: list[Any] = surface["templates"]

    lines: list[str] = [_GENERATED_HEADER, "", "# inkscape-mcp — full manifest (llms-full.txt)", ""]
    lines.append(PROJECT_ONE_LINER)
    lines.append("")
    lines.append(
        f"Surface: {len(tools)} tools, {len(prompts)} prompts, "
        f"{len(resources) + len(templates)} resources."
    )
    lines.append("")

    lines.append("## Run & transport")
    lines += _run_config_lines()
    lines.append("")

    lines.append("## Environment / configuration")
    lines += _env_config_lines()
    lines.append("")

    lines.append("## Tools")
    for label, bucket in _grouped_tools(tools):
        lines.append("")
        lines.append(f"### {label}")
        for tool in bucket:
            lines.append("")
            lines.append(f"#### {tool.name}")
            lines.append(f"Risk class: {_risk_class(tool)}")
            params = _key_params(tool)
            if params:
                rendered = ", ".join(f"{n}: {t}" for n, t in params)
                lines.append(f"Key params: {rendered}")
            else:
                lines.append("Key params: (none)")
            lines.append("Description:")
            for desc_line in (tool.description or "(no description)").splitlines():
                lines.append(f"    {desc_line}".rstrip())
    lines.append("")

    lines += _ops_full_lines(_op_reference())
    lines.append("")

    lines.append("## Prompts")
    for prompt in sorted(prompts, key=lambda p: p.name):
        args = getattr(prompt, "arguments", None) or []
        arg_names = ", ".join(getattr(a, "name", str(a)) for a in args) or "(none)"
        lines.append("")
        lines.append(f"#### {prompt.name}")
        lines.append(f"Args: {arg_names}")
        lines.append(f"Description: {(prompt.description or '').strip()}")
    lines.append("")

    lines.append("## Resources")
    for res in sorted(resources, key=lambda r: str(r.uri)):
        lines.append("")
        lines.append(f"#### {res.uri}")
        lines.append(f"Description: {(getattr(res, 'description', '') or '').strip()}")
    for tmpl in sorted(templates, key=lambda t: str(t.uri_template)):
        lines.append("")
        lines.append(f"#### {tmpl.uri_template}")
        lines.append(f"Description: {(getattr(tmpl, 'description', '') or '').strip()}")
    lines.append("")
    return "\n".join(lines)


def generate(dest: Path | None = None) -> dict[str, str]:
    """Generate both files' content; write them under ``dest`` (default: server root).

    Returns a mapping ``{filename: content}`` so callers (e.g. the drift-guard test) can compare
    against the on-disk copies without re-reading.
    """
    target = dest or SERVER_ROOT
    surface = asyncio.run(_collect())
    out = {
        "llms.txt": render_index(surface),
        "llms-full.txt": render_full(surface),
    }
    for name, content in out.items():
        (target / name).write_text(content, encoding="utf-8")
    return out


def main() -> int:
    out = generate()
    for name, content in out.items():
        path = SERVER_ROOT / name
        print(f"wrote {path} ({len(content)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
