"""Process configuration and limits (workspace model).

`Settings` holds the immutable sandbox boundary (resolved workspace roots) and every
operator-tunable limit. All values come from the environment on first access and are
cached for the process lifetime via `get_settings()`. Roots that are missing, not a
directory, or not read+writable are EXCLUDED and recorded in `root_diagnostics`; the
server never auto-creates a root and never falls back to CWD or home.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from inkscape_mcp.logging_setup import get_logger

#: Environment variable holding the OS-path-separator-delimited list of workspace roots.
ENV_WORKSPACE_ROOTS = "INKSCAPE_MCP_WORKSPACE_ROOTS"

# Per-limit environment override keys.
ENV_MAX_INPUT_BYTES = "INKSCAPE_MCP_MAX_INPUT_BYTES"
ENV_MAX_EXPORT_PX = "INKSCAPE_MCP_MAX_EXPORT_PX"
ENV_MAX_OUTPUT_BYTES = "INKSCAPE_MCP_MAX_OUTPUT_BYTES"
ENV_PROCESS_TIMEOUT_S = "INKSCAPE_MCP_PROCESS_TIMEOUT_S"
ENV_MAX_PROCS = "INKSCAPE_MCP_MAX_PROCS"
ENV_SNAPSHOT_KEEP_N = "INKSCAPE_MCP_SNAPSHOT_KEEP_N"
ENV_SNAPSHOT_KEEP_DAYS = "INKSCAPE_MCP_SNAPSHOT_KEEP_DAYS"
ENV_SNAPSHOT_HARD_MAX_N = "INKSCAPE_MCP_SNAPSHOT_HARD_MAX_N"
ENV_SNAPSHOT_HARD_MAX_BYTES = "INKSCAPE_MCP_SNAPSHOT_HARD_MAX_BYTES"
ENV_ARTIFACT_KEEP_DAYS = "INKSCAPE_MCP_ARTIFACT_KEEP_DAYS"
ENV_ARTIFACT_MAX_BYTES = "INKSCAPE_MCP_ARTIFACT_MAX_BYTES"
ENV_ARTIFACT_MAX_BYTES_PER_DOC = "INKSCAPE_MCP_ARTIFACT_MAX_BYTES_PER_DOC"

# Live-view render cache + loop-frame retention knobs (E8-06). The cache is a bounded LRU; the
# loop-frame retention caps fold into the EXPLICIT E1-10 sweep (never an implicit mutating-tool side
# effect). Each value is floored in `Settings`/`live/cache.py` so a degenerate config can never turn
# a bound into "unbounded" growth.
ENV_LIVE_CACHE_MAX_ENTRIES = "INKSCAPE_MCP_LIVE_CACHE_MAX_ENTRIES"
ENV_LIVE_CACHE_MAX_BYTES = "INKSCAPE_MCP_LIVE_CACHE_MAX_BYTES"
ENV_LIVE_COALESCE_BUDGET_MS = "INKSCAPE_MCP_LIVE_COALESCE_BUDGET_MS"
ENV_LIVE_FRAME_KEEP_DAYS = "INKSCAPE_MCP_LIVE_FRAME_KEEP_DAYS"
ENV_LIVE_FRAME_MAX_BYTES = "INKSCAPE_MCP_LIVE_FRAME_MAX_BYTES"

#: Master live-mode gate (E3 / X1). Live mode is ON by default (operator-chosen); set this to a
#: falsy value (`0`/`false`/`no`/`off`) to opt OUT. When off, `live_connect` refuses cleanly and
#: headless is unaffected. This is the hard operator switch; `live_connect` / `live_disconnect` are
#: the per-session runtime toggle on top of it.
ENV_LIVE_ENABLED = "INKSCAPE_MCP_LIVE_ENABLED"

#: Headless shell engine gate (E12 / ADR-007). Selects the engine transport for the Inkscape-engine
#: ops (render/export/path/boolean/action-chain): `per_call` spawns a fresh `inkscape …` per call
#: (the default, always-correct baseline); `shell` routes those ops through one warm, long-lived
#: `inkscape --shell` worker per document, with an AUTOMATIC per-call fallback on any fault so it
#: can never regress correctness. Any value other than `shell` (incl. unset/empty/garbage) floors
#: to `per_call`. This is a private headless worker, NOT a channel to the user's live GUI (§4.4).
ENV_ENGINE_MODE = "INKSCAPE_MCP_ENGINE_MODE"

#: Max concurrent warm `inkscape --shell` worker processes the `EngineManager` keeps alive (E12-01).
#: LRU-evicted above this cap. Floored at 1 so a degenerate value can never disable the bound.
ENV_ENGINE_MAX_PROCESSES = "INKSCAPE_MCP_ENGINE_MAX_PROCESSES"

#: Idle timeout (seconds) after which an unused warm shell worker is shut down (E12-01). Floored at
#: 1.0s. The manager reaps a worker idle longer than this before reusing the slot.
ENV_ENGINE_IDLE_TIMEOUT_S = "INKSCAPE_MCP_ENGINE_IDLE_TIMEOUT_S"

#: The two valid `engine_mode` values. Anything else floors to `per_call` (the safe baseline).
ENGINE_MODE_PER_CALL = "per_call"
ENGINE_MODE_SHELL = "shell"

#: Tool-disclosure PROFILE gate (E18-03). Opt-in knob that NARROWS the visible `tools/list` to a
#: curated essential core, cutting the per-turn model-context cost of the default ~85-tool surface.
#: `full` (default) keeps the flag-allowed surface unchanged; `core` narrows to the essential
#: authoring set (open/inspect/find/create/style/transform/export/snapshot). Parsed via
#: `_env_choice` so a stray/garbage value floors to `full` — the profile can ONLY narrow within the
#: flag-allowed
#: surface (sec.12 / ADR-003), never widen past what the live/advanced flags expose.
ENV_TOOL_PROFILE = "INKSCAPE_MCP_TOOL_PROFILE"

#: The two valid `tool_profile` values. Anything else floors to `full` (the unchanged surface).
TOOL_PROFILE_FULL = "full"
TOOL_PROFILE_CORE = "core"

#: Tool-DESCRIPTION mode (E20-03). Orthogonal to `tool_profile`: the profile trims tool COUNT, this
#: trims description LENGTH. `full` (default) serves the complete E15-02 6-part docstring as each
#: tool's `tools/list` description; `short` serves a DERIVED short form (the first "what it does"
#: line + the `Risk class:` line) to cut the heavy per-turn `tools/list` token cost (~86% of the
#: description bytes). The short form is always DERIVED from the canonical docstring — never a
#: second hand-maintained copy — and the JSON `inputSchema` (param names/types) is unaffected, so
#: callers keep full argument detail. Parsed via `_env_choice` so a stray value floors to `full`.
ENV_TOOL_DESC = "INKSCAPE_MCP_TOOL_DESC"

#: The two valid `tool_desc` values. Anything else floors to `full` (the complete description).
TOOL_DESC_FULL = "full"
TOOL_DESC_SHORT = "short"

#: Advanced-mode gate for the optional raw-action tool (E6-03 / ADR-003 / sec.12). OFF by default —
#: the `run_raw_action` escape hatch is refused entirely unless an operator sets this to a truthy
#: value (`1`/`true`/`yes`/`on`). It is the explicit opt-in for the single, HIGH-risk,
#: allowlist-and-map-gated raw Inkscape Action runner deferred from the MVP. Never client-supplied;
#: the tool checks it BEFORE doing anything. Enabling it does NOT widen the allowlist — every Action
#: still has to be allowlisted AND present in the version-keyed capability map AND charset-safe.
ENV_RAW_ACTION_ENABLED = "INKSCAPE_MCP_RAW_ACTION_ENABLED"

#: Action/extension execution allowlists (E6-02 / ADR-003 / sec.12). These are the SERVER-SIDE,
#: operator-controlled sets of Inkscape Action ids (resp. extension ids) that may ever execute via a
#: validated Action chain. They are NEVER client-supplied — a model can only request items already
#: in this set, and only those that also exist in the version-keyed capability map. Each var is an
#: OS-path-separator-delimited list ADDED to the built-in defaults below; it cannot remove a default
#: or open the surface to arbitrary execution. Empty/unset ⇒ defaults only.
ENV_ACTION_ALLOWLIST = "INKSCAPE_MCP_ACTION_ALLOWLIST"
ENV_EXTENSION_ALLOWLIST = "INKSCAPE_MCP_EXTENSION_ALLOWLIST"

#: Built-in default Action allowlist (E6-02). A conservative, non-destructive set of selection +
#: structure + path Actions known to exist on the targeted Inkscape line. Destructive geometry has
#: its own dedicated typed tools (E6-01); this set is the controlled chain surface E6-03 builds on.
#: `select-by-id` is REQUIRED as the chain's targeting primitive. Everything outside this set (and
#: the operator additions) is refused — there is no arbitrary-Action passthrough here.
DEFAULT_ACTION_ALLOWLIST: tuple[str, ...] = (
    "select-by-id",
    "select-all",
    "select-clear",
    "selection-group",
    "selection-ungroup",
    "object-to-path",
    "path-union",
    "path-difference",
    "path-intersection",
    "path-combine",
    "path-break-apart",
    "path-simplify",
)

#: Built-in default extension allowlist (E6-02). Empty by default — no `inkex` extension is enabled
#: for execution until an operator opts one in. Discovery is read-only and unaffected by this set.
DEFAULT_EXTENSION_ALLOWLIST: tuple[str, ...] = ()


class Settings(BaseModel):
    """Immutable process settings: sandbox roots + all operator-tunable limits.

    Built once from the environment by `get_settings()`. `workspace_roots` are canonical
    real paths (resolved at build time); `root_diagnostics` carries human-readable
    messages for roots that were configured but excluded as unusable.
    """

    model_config = ConfigDict(frozen=True)

    workspace_roots: list[Path] = Field(default_factory=list)
    root_diagnostics: list[str] = Field(default_factory=list)

    max_input_bytes: int = 50 * 1024 * 1024
    max_export_px: int = 8192
    max_output_bytes: int = 100 * 1024 * 1024
    process_timeout_s: float = 60.0
    max_procs: int = 2

    snapshot_keep_n: int = 50
    snapshot_keep_days: int = 30
    snapshot_hard_max_n: int = 500
    snapshot_hard_max_bytes: int = 5 * 1024**3

    artifact_keep_days: int = 14
    artifact_max_bytes: int = 2 * 1024**3
    artifact_max_bytes_per_doc: int = 512 * 1024**2

    #: Live-view render cache bounds (E8-06). The per-session frame cache holds at most
    #: `live_cache_max_entries` frames and `live_cache_max_bytes` total; eviction drops the
    #: least-recently-used. Both are floored in `live/cache.py` so a degenerate value can never
    #: disable the bound. The cache key includes the E8-03 document-revision digest, so a stale
    #: frame can never be served after the document changes.
    live_cache_max_entries: int = 64
    live_cache_max_bytes: int = 256 * 1024**2

    #: Coalescing latency budget (ms) for the live render cache (E8-06). Within this window a
    #: repeated IDENTICAL-key render request returns the just-cached frame instead of launching
    #: another render, so a burst of same-state requests cannot thrash the renderer. `0` = off.
    live_coalesce_budget_ms: float = 200.0

    #: Loop-frame retention caps (E8-06). Rasterized live frames accumulate under the root-scoped
    #: live artifacts dir; the EXPLICIT E1-10 sweep (`sweep_all_roots` / `prune_snapshots`) prunes
    #: them by age (`live_frame_keep_days`) and total bytes (`live_frame_max_bytes`, newest kept).
    #: NEVER pruned implicitly by a mutating tool (project hard rule).
    live_frame_keep_days: int = 7
    live_frame_max_bytes: int = 512 * 1024**2

    #: Master live-mode gate (E3 / X1). Default ON (operator-chosen) — headless never depends on it.
    live_enabled: bool = True

    #: Headless shell engine settings (E12 / ADR-007). `engine_mode` is `per_call` (default) or
    #: `shell`; `engine_max_processes` caps warm workers (LRU-evicted); `engine_idle_timeout_s`
    #: reaps idle workers. `shell` always carries an automatic per-call fallback, so a misconfig
    #: degrades to the always-correct baseline rather than failing.
    engine_mode: str = ENGINE_MODE_PER_CALL
    engine_max_processes: int = 2
    engine_idle_timeout_s: float = 300.0

    #: Tool-disclosure profile (E18-03). `full` (default) leaves the flag-allowed surface unchanged;
    #: `core` narrows `tools/list` to the curated essential set. Floored to `full` on any stray
    #: value; gating may ONLY narrow within the flag-allowed surface (sec.12 / ADR-003).
    tool_profile: str = TOOL_PROFILE_FULL

    #: Tool-description mode (E20-03). `full` (default) serves the complete docstring as each tool's
    #: `tools/list` description; `short` serves a derived short form (summary + risk line) to cut
    #: per-turn token cost. Floored to `full` on any stray value. Orthogonal to `tool_profile`.
    tool_desc: str = TOOL_DESC_FULL

    #: Advanced-mode gate for the optional raw-action tool (E6-03 / ADR-003). Default OFF — the
    #: `run_raw_action` escape hatch refuses entirely unless an operator opts in. Enabling it does
    #: not widen the allowlist or relax the version-map/charset/HIGH+approval checks.
    raw_action_enabled: bool = False

    #: Server-side Action/extension execution allowlists (E6-02). Frozen, operator-controlled,
    #: never client-supplied. Default to the built-in DEFAULT_*_ALLOWLIST; env additions are merged
    #: in `get_settings`. The execution layer refuses any Action/extension not in these sets.
    action_allowlist: frozenset[str] = frozenset(DEFAULT_ACTION_ALLOWLIST)
    extension_allowlist: frozenset[str] = frozenset(DEFAULT_EXTENSION_ALLOWLIST)


def _resolve_roots(raw: str | None) -> tuple[list[Path], list[str]]:
    """Resolve the configured root list to (usable_real_paths, diagnostics).

    Each entry is resolved once to its canonical real path. A root that is missing, not a
    directory, or not read+writable is excluded and explained in the diagnostics list.
    Never auto-created; never falls back to CWD or home.
    """
    roots: list[Path] = []
    diagnostics: list[str] = []
    if not raw:
        return roots, diagnostics

    seen: set[Path] = set()
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        try:
            resolved = Path(entry).resolve(strict=True)
        except (FileNotFoundError, OSError):
            diagnostics.append(f"workspace root does not exist: {entry!r}")
            continue
        if not resolved.is_dir():
            diagnostics.append(f"workspace root is not a directory: {entry!r}")
            continue
        if not os.access(resolved, os.R_OK | os.W_OK):
            diagnostics.append(f"workspace root is not readable+writable: {entry!r}")
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots, diagnostics


def _env_int(key: str, default: int, minimum: int | None = None) -> int:
    """Parse an int env override; fall back to `default` if unset, unparseable, or below
    `minimum`. The floor stops a misconfigured `0`/negative from silently disabling a
    safety gate (e.g. a non-positive size cap that would pass every file)."""
    val = os.environ.get(key)
    if val is None or val.strip() == "":
        return default
    try:
        parsed = int(val)
    except ValueError:
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


def _env_bool(key: str, default: bool) -> bool:
    """Parse a boolean env override. Truthy = {1,true,yes,on} (case-insensitive); everything
    else (including unset/empty) falls back to `default`. Used for the live-mode master gate so
    a stray value can never silently enable a restricted feature."""
    val = os.environ.get(key)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_choice(key: str, default: str, choices: tuple[str, ...]) -> str:
    """Parse an enumerated string env override; fall back to `default` if unset or not in `choices`.

    Case-insensitive + whitespace-trimmed. Used for the engine-mode gate so a stray/garbage value
    can never select an unintended transport — it floors to the safe default (`per_call`)."""
    val = os.environ.get(key)
    if val is None or val.strip() == "":
        return default
    candidate = val.strip().lower()
    return candidate if candidate in choices else default


def _env_float(key: str, default: float, minimum: float | None = None) -> float:
    """Parse a float env override; fall back to `default` if unset, unparseable, or below
    `minimum`. The floor stops a non-positive timeout from killing every subprocess."""
    val = os.environ.get(key)
    if val is None or val.strip() == "":
        return default
    try:
        parsed = float(val)
    except ValueError:
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


#: An Action/extension token is conservatively constrained to letters, digits, and the few
#: punctuation chars Inkscape uses in action ids (`.`, `-`, `_`, namespaced `:`). Anything else in
#: an operator-supplied allowlist entry is dropped — a malformed token can never enter the set that
#: gates execution. The chain layer additionally cross-checks each token against the version map.
_ALLOWLIST_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _env_token_set(key: str, defaults: tuple[str, ...]) -> frozenset[str]:
    """Merge an OS-path-separator-delimited env list of Action/extension tokens onto `defaults`.

    The env value can only ADD operator-approved tokens to the built-in defaults — it cannot remove
    a default or inject an arbitrary string: each entry must match `_ALLOWLIST_TOKEN_RE` or it is
    silently dropped. Unset/empty ⇒ just the defaults. The result is frozen and never sourced from a
    tool argument (the allowlist is server-side, not client-supplied — sec.12 / ADR-003)."""
    tokens: set[str] = set(defaults)
    raw = os.environ.get(key)
    if raw:
        for entry in raw.split(os.pathsep):
            entry = entry.strip()
            if not entry:
                continue
            if _ALLOWLIST_TOKEN_RE.match(entry):
                tokens.add(entry)
            else:
                # Drop a malformed token (safe direction) but make the misconfiguration visible.
                get_logger("config").warning(
                    "dropped malformed allowlist token", extra={"env": key}
                )
    return frozenset(tokens)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-cached `Settings` singleton (reads env on first call).

    Reset for tests with `get_settings.cache_clear()`.
    """
    roots, diagnostics = _resolve_roots(os.environ.get(ENV_WORKSPACE_ROOTS))
    return Settings(
        workspace_roots=roots,
        root_diagnostics=diagnostics,
        max_input_bytes=_env_int(ENV_MAX_INPUT_BYTES, 50 * 1024 * 1024, minimum=1),
        max_export_px=_env_int(ENV_MAX_EXPORT_PX, 8192, minimum=1),
        max_output_bytes=_env_int(ENV_MAX_OUTPUT_BYTES, 100 * 1024 * 1024, minimum=1),
        process_timeout_s=_env_float(ENV_PROCESS_TIMEOUT_S, 60.0, minimum=1.0),
        max_procs=_env_int(ENV_MAX_PROCS, 2, minimum=1),
        snapshot_keep_n=_env_int(ENV_SNAPSHOT_KEEP_N, 50),
        snapshot_keep_days=_env_int(ENV_SNAPSHOT_KEEP_DAYS, 30),
        snapshot_hard_max_n=_env_int(ENV_SNAPSHOT_HARD_MAX_N, 500),
        snapshot_hard_max_bytes=_env_int(ENV_SNAPSHOT_HARD_MAX_BYTES, 5 * 1024**3),
        artifact_keep_days=_env_int(ENV_ARTIFACT_KEEP_DAYS, 14),
        artifact_max_bytes=_env_int(ENV_ARTIFACT_MAX_BYTES, 2 * 1024**3),
        artifact_max_bytes_per_doc=_env_int(ENV_ARTIFACT_MAX_BYTES_PER_DOC, 512 * 1024**2),
        live_cache_max_entries=_env_int(ENV_LIVE_CACHE_MAX_ENTRIES, 64, minimum=1),
        live_cache_max_bytes=_env_int(ENV_LIVE_CACHE_MAX_BYTES, 256 * 1024**2, minimum=1),
        live_coalesce_budget_ms=_env_float(ENV_LIVE_COALESCE_BUDGET_MS, 200.0, minimum=0.0),
        live_frame_keep_days=_env_int(ENV_LIVE_FRAME_KEEP_DAYS, 7),
        live_frame_max_bytes=_env_int(ENV_LIVE_FRAME_MAX_BYTES, 512 * 1024**2),
        live_enabled=_env_bool(ENV_LIVE_ENABLED, True),
        engine_mode=_env_choice(
            ENV_ENGINE_MODE, ENGINE_MODE_PER_CALL, (ENGINE_MODE_PER_CALL, ENGINE_MODE_SHELL)
        ),
        engine_max_processes=_env_int(ENV_ENGINE_MAX_PROCESSES, 2, minimum=1),
        engine_idle_timeout_s=_env_float(ENV_ENGINE_IDLE_TIMEOUT_S, 300.0, minimum=1.0),
        tool_profile=_env_choice(
            ENV_TOOL_PROFILE, TOOL_PROFILE_FULL, (TOOL_PROFILE_FULL, TOOL_PROFILE_CORE)
        ),
        tool_desc=_env_choice(ENV_TOOL_DESC, TOOL_DESC_FULL, (TOOL_DESC_FULL, TOOL_DESC_SHORT)),
        raw_action_enabled=_env_bool(ENV_RAW_ACTION_ENABLED, False),
        action_allowlist=_env_token_set(ENV_ACTION_ALLOWLIST, DEFAULT_ACTION_ALLOWLIST),
        extension_allowlist=_env_token_set(ENV_EXTENSION_ALLOWLIST, DEFAULT_EXTENSION_ALLOWLIST),
    )
