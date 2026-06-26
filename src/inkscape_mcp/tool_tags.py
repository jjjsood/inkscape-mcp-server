"""Central tool-tag map + flag-driven progressive disclosure.

Single source of truth for the DOMAIN and RISK tags carried by every registered `@mcp.tool`, and
the boot-time pass that drives PROGRESSIVE DISCLOSURE from the EXISTING config flags by EXCLUDING
tagged tools from `tools/list`. A default MCP client sees a smaller core surface and opts into the
advanced / live groups by flipping the operator flags — instead of always receiving all ~97 tools.

Design (mirrors `tool_annotations.py`, ADR-002 / project risk classes):

- DOMAIN tag (exactly one per tool): `create` / `edit` / `transform` / `paths` / `export` / `live` /
  `actions` / `system` / `quality`. Derived from the tool's defining MODULE — one residual domain
  (`edit`) catches the structural-edit / document / discovery modules that have no dedicated bucket.
- RISK tag (exactly one per tool): the SAME risk vocabulary uses, parsed from the docstring
  `Risk class:` line via `tools.system._risk_class` (`low` / `medium` / `high` / `restricted`, or
  `unknown` if absent). There is exactly ONE risk vocabulary in the codebase — no second source.

Gating (security-critical, sec.12 / ADR-003):

- `live_enabled` OFF  → exclude every `live`-tagged tool.
- advanced-mode OFF (`raw_action_enabled` false) → exclude the ADR-003 hatch group: `run_raw_action`
  plus every `paths`- and `actions`-tagged tool.
- `tool_profile == "core"` → exclude every tool OUTSIDE the curated `_CORE_MODULES` set, an
  opt-in minimal profile that cuts the default per-turn model-context cost. STRICT SUBSET only.
- Gating may ONLY NARROW the surface. With the flags at their widest the FULL surface returns; a
  tool the flags would hide can never be made visible via tags. Implemented with FastMCP
  `disable(...)` visibility transforms (which drop the matching tools from `list_tools()`), applied
  AFTER `apply_annotations`.
  The pass is idempotent + re-evaluatable: it removes its own prior transforms before re-applying,
  so re-running `register_tools()` under a different flag config (as the tests do) yields exactly
  the surface the CURRENT flags allow — never a stale union.

Tags are static labels only; no host path ever enters a tag (sec.12).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

#: Domain tag for every tool, keyed by the tool's defining module (the last path segment of
#: `fn.__module__`). One domain per module. Modules without a dedicated domain bucket
#: (structural edit / document / compose / find / snapshots / optimize / discovery / validate /
#: save) fold into the residual `edit` domain.
_MODULE_DOMAIN: dict[str, str] = {
    "create": "create",
    "transform": "transform",
    "paths": "paths",
    "export": "export",
    "export_batch": "export",
    "profiles": "export",
    "live": "live",
    "actions": "actions",
    "system": "system",
    "quality": "quality",
    # residual `edit` domain — modules with no dedicated bucket
    "style": "edit",
    "text_object": "edit",
    "compose": "edit",
    "dom": "edit",
    "batch": "edit",
    "transform_objects": "edit",
    "document": "edit",
    "find": "edit",
    "snapshots": "edit",
    "optimize": "edit",
    "discover": "edit",
    "validate": "edit",
    "save": "edit",
}

#: The canonical set of domain tags (one per tool). Exposed for tests + docs.
DOMAIN_TAGS: frozenset[str] = frozenset(_MODULE_DOMAIN.values())

#: The canonical set of risk tags (one per tool); mirrors the risk vocabulary.
RISK_TAGS: frozenset[str] = frozenset({"low", "medium", "high", "restricted", "unknown"})

#: Domain tags whose tools form the ADR-003 advanced-mode hatch group (`run_raw_action` lives in
#: `actions`; the `paths` geometry tools + the controlled Action surface in `actions`). Excluded
#: from `tools/list` unless advanced mode (`raw_action_enabled`) is ON.
_ADVANCED_DOMAIN_TAGS: frozenset[str] = frozenset({"paths", "actions"})

#: The single ADR-003 escape-hatch tool excluded BY NAME when advanced mode is off (it is already
#: refused at call time when off; this also hides it from the default surface). Belt-and-braces with
#: the `actions` domain tag — naming it explicitly keeps the intent legible.
_ADVANCED_TOOL_NAMES: frozenset[str] = frozenset({"run_raw_action"})

#: Defining-MODULE leaves whose tools form the curated `core` disclosure profile: the
#: essential authoring workflow — open/inspect/create document (`document`), find (`find`),
#: create-* shapes/gradients/groups (`create`), style (`style`), transform (`transform`), export
#: (`export`), snapshot (`snapshots`). Keyed by module leaf (not domain tag) because the residual
#: `edit` domain mixes core and non-core modules; a new tool added to one of these modules joins
#: core automatically. With `INKSCAPE_MCP_TOOL_PROFILE=core`, every tool OUTSIDE these modules is
#: disabled — a STRICT SUBSET of the flag-allowed surface (gating only narrows; sec.12 / ADR-003).
_CORE_MODULES: frozenset[str] = frozenset(
    {"document", "find", "create", "style", "transform", "export", "snapshots"}
)


def _module_leaf(component: object) -> str:
    """Defining-module leaf for a Tool component (`a.b.create` → `create`), or `""`."""
    fn = getattr(component, "fn", None)  # `fn` lives on FunctionTool, not the base Tool
    return (getattr(fn, "__module__", "") or "").rsplit(".", 1)[-1]


def _domain_for(module_leaf: str) -> str:
    """Domain tag for a tool defined in module `module_leaf` (residual `edit` if unmapped)."""
    return _MODULE_DOMAIN.get(module_leaf, "edit")


def tags_for(module_leaf: str, risk: str) -> set[str]:
    """Build the {domain, risk} tag set for one tool from its module + parsed risk class."""
    risk_tag = risk if risk in RISK_TAGS else "unknown"
    return {_domain_for(module_leaf), risk_tag}


#: Marker carried on every visibility transform this module appends, so the gating pass can find
#: and drop ONLY its own transforms on re-run (never another subsystem's). Set as an attribute on
#: the transform object — FastMCP does not inspect it.
_GATING_MARKER = "_e17_02_gating"


def apply_tags(app: FastMCP) -> None:
    """Stamp every registered tool on `app` with its {domain, risk} tag set, in place.

    A post-registration pass over the local provider's SYNC component registry (same style as
    `apply_annotations`), so tags come from ONE central map and need no per-`@mcp.tool` hand
    tagging. Risk is parsed with the canonical `_risk_class` regex (one risk vocabulary). Idempotent
    — re-running overwrites the tags with the same derived values.
    """
    from fastmcp.tools import Tool

    from inkscape_mcp.tools.system import _risk_class

    for component in app._local_provider._components.values():
        if not isinstance(component, Tool):
            continue
        module_leaf = _module_leaf(component)
        risk = _risk_class(component.description)
        component.tags = tags_for(module_leaf, risk)


def _clear_gating_transforms(app: FastMCP) -> None:
    """Drop only the visibility transforms this module previously appended (re-run safety)."""
    app._transforms = [t for t in app._transforms if not getattr(t, _GATING_MARKER, False)]


def _disable(app: FastMCP, *, names: set[str] | None = None, tags: set[str] | None = None) -> None:
    """Append a marked `disable(...)` visibility transform so it can be cleared on re-run."""
    app.disable(names=names, tags=tags)
    setattr(app._transforms[-1], _GATING_MARKER, True)


def _non_core_tool_names(app: FastMCP) -> set[str]:
    """Names of every registered Tool whose defining module is OUTSIDE `_CORE_MODULES`."""
    from fastmcp.tools import Tool

    return {
        c.name
        for c in app._local_provider._components.values()
        if isinstance(c, Tool) and _module_leaf(c) not in _CORE_MODULES
    }


def apply_disclosure(app: FastMCP) -> None:
    """Apply flag-driven progressive disclosure: NARROW `tools/list` per the current config flags.

    Reads the live `Settings` (so a test that flips a flag + clears the settings cache, then
    re-runs `register_tools()`, gets the surface the CURRENT flags allow). Removes any gating
    transforms from a prior pass first, then:

    - `live_enabled` off  → `disable(tags={"live"})`.
    - advanced-mode off   → `disable(tags={"paths", "actions"})` + `disable` of `run_raw_action`.
    - `tool_profile == "core"` → `disable(names=…)` of every tool OUTSIDE `_CORE_MODULES`.

    With all flags at their widest (live on, advanced on, profile `full`), NO disable transform is
    added — the FULL surface returns. Gating can ONLY narrow: it appends `disable` transforms; it
    never adds an `enable`/widen transform (sec.12). The `core` profile disables by NAME within the
    registered set, so it is always a STRICT SUBSET of the flag-allowed surface — it can never
    expose a tool the live/advanced flags hide.
    """
    from inkscape_mcp.config import TOOL_PROFILE_CORE, get_settings

    settings = get_settings()
    _clear_gating_transforms(app)

    if not settings.live_enabled:
        _disable(app, tags={"live"})
    if not settings.raw_action_enabled:
        _disable(app, tags=set(_ADVANCED_DOMAIN_TAGS))
        _disable(app, names=set(_ADVANCED_TOOL_NAMES))
    if settings.tool_profile == TOOL_PROFILE_CORE:
        non_core = _non_core_tool_names(app)
        if non_core:
            _disable(app, names=non_core)


def apply_tags_and_disclosure(app: FastMCP) -> None:
    """Tag every tool, then apply flag-driven disclosure — the single boot entry point."""
    apply_tags(app)
    apply_disclosure(app)
