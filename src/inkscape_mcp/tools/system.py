"""System diagnostics tools (thin MCP layer).

`list_capabilities` / `diagnose_runtime` expose the runtime capability matrix
(:mod:`inkscape_mcp.runtime.capabilities`); `stat_artifact` / `stat_artifacts` expose the read-only
artifact stat (:mod:`inkscape_mcp.workspace.stat`).

All tools here are read-only (Risk class: low) — no Operation Record / snapshot required (ADR-004).
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from inkscape_mcp.logging_setup import get_logger, log_file_io
from inkscape_mcp.runtime.capabilities import (
    _risk_class,
    get_cached_capabilities,
    refresh_capabilities,
    with_registry_tools,
)
from inkscape_mcp.runtime.probe import Capabilities
from inkscape_mcp.server import mcp
from inkscape_mcp.workspace.limits import LimitExceeded
from inkscape_mcp.workspace.paths import SandboxViolation
from inkscape_mcp.workspace.stat import ArtifactStat, ArtifactStatSet, stat_many, stat_one

__all__ = [
    "ArtifactStat",
    "ArtifactStatSet",
    "_risk_class",
    "diagnose_runtime",
    "get_cached_capabilities",
    "list_capabilities",
    "refresh_capabilities",
    "stat_artifact",
    "stat_artifacts",
    "with_registry_tools",
]

_logger = get_logger("tools.system")


@mcp.tool
def diagnose_runtime() -> Capabilities:
    """Probe the local Inkscape + Python runtime fresh and return the capability matrix.

    When to use: to FORCE a fresh probe (e.g. after installing Inkscape/fonts). For a cheap cached
    read use `list_capabilities`; for live-transport detail use `check_live_support`.

    Key params: none. Re-runs the probe every call and refreshes the cache that `list_capabilities`
    and `inkscape://runtime/capabilities` serve.

    Return shape: `Capabilities` — Inkscape version, available actions, export formats, data dirs,
    inkex, DBus/live transport availability, fonts, the curated `intents` map, and the authoritative
    MCP tool surface (`tool_count` + `tools`: name + one-line purpose + risk class, from the live
    registry). Missing backends are reported in `notes`, never crashed.

    Example: `diagnose_runtime()`

    Risk class: low (read-only probe).
    """
    return refresh_capabilities()


@mcp.tool
def list_capabilities() -> Capabilities:
    """Return the cached runtime capability matrix (probed once, then reused).

    When to use: the cheap default for "what can this host/server do". To FORCE a re-probe use
    `diagnose_runtime`; to map a single goal to a tool use `how_do_i`.

    Key params: none.

    Return shape: `Capabilities` — same shape as `diagnose_runtime`, served from cache. Includes an
    `intents` section: the curated natural-language goal → tool(s) map (the same map `how_do_i`
    matches against) so an agent can browse "which tool does X" without one call per goal. Also
    carries the authoritative MCP tool surface: `tool_count` (the one true count of registered
    `@mcp.tool`s) and `tools` (name + one-line purpose + risk class), sourced from the live
    registry — so agents read one number instead of deriving it.

    Example: `list_capabilities()`

    Risk class: low (read-only).
    """
    return get_cached_capabilities()


def _map_stat_failure(exc: Exception) -> ToolError:
    """Map a sandbox/limit failure to a stable, host-path-free `ToolError` (sec.12)."""
    if isinstance(exc, SandboxViolation):
        _logger.error("stat_artifact rejected", extra={"detail": exc.detail})
        return ToolError(str(exc))
    if isinstance(exc, LimitExceeded):
        _logger.error("stat_artifact over limit", extra={"detail": str(exc)})
        return ToolError("artifact exceeds the configured size limit")
    return ToolError("artifact could not be stat'd")  # pragma: no cover - defensive


@mcp.tool
def stat_artifact(path: str) -> ArtifactStat:
    """Return the on-disk byte size + sha256 digest of one sandboxed artifact.

    When to use: to VERIFY what you wrote (an export, a render, a saved SVG) — its exact byte size
    and content digest — without a `wc -c` / `sha256sum` Bash fallback. For a whole SET (and an
    aggregate byte total) use `stat_artifacts`; for image pixel dimensions read the producing
    tool's result fields instead.

    Key params: `path` may be workspace-RELATIVE (anchored to the first workspace root, matching
    `open_document` / `save_document_as`) or absolute; either is sandbox-validated and a
    `../`-escape, an absolute path outside the workspace, or a symlink whose target leaves the
    sandbox is rejected with `path rejected: outside workspace`. The file must exist and be within
    the configured size limit; the sha256 is computed streaming so a large file is bounded in
    memory.

    Return shape: `ArtifactStat` — `{path, bytes, sha256}` where `path` is the WORKSPACE-RELATIVE
    POSIX path (never a host path) and `sha256` is the lowercase hex digest.

    Example: `stat_artifact("dist/logo.png")`

    Risk class: low (read-only stat; nothing is mutated, no Operation Record / snapshot).
    """
    try:
        stat = stat_one(path)
    except (SandboxViolation, LimitExceeded) as exc:
        raise _map_stat_failure(exc) from exc
    log_file_io(_logger, action="stat_artifact", artifact=stat.path, bytes=stat.bytes)
    return stat


@mcp.tool
def stat_artifacts(paths: list[str]) -> ArtifactStatSet:
    """Stat a SET of sandboxed artifacts: per-file size + sha256 plus an aggregate byte total.

    When to use: to verify a whole produced collection (an icon set, a `dist/` tree) and read its
    TOTAL byte budget in one call — the readback half of a batch export, without a `du -cb`. For a
    single file use `stat_artifact`.

    Key params: `paths` is a non-empty list; each entry is resolved EXACTLY as `stat_artifact`
    resolves its `path` (workspace-relative or absolute, sandbox + symlink validated, size-capped).
    The first entry that escapes the sandbox or exceeds the size limit fails the whole call with a
    stable message — nothing partial is returned.

    Return shape: `ArtifactStatSet` — `{artifacts: [{path, bytes, sha256}], total_bytes, count}`
    where `total_bytes` is the sum of the per-file sizes and every `path` is workspace-relative
    (never a host path).

    Example: `stat_artifacts(["dist/16.png", "dist/32.png", "dist/64.png"])`

    Risk class: low (read-only stat; nothing is mutated, no Operation Record / snapshot).
    """
    if not paths:
        raise ToolError("stat_artifacts requires at least one path")
    try:
        result = stat_many(paths)
    except (SandboxViolation, LimitExceeded) as exc:
        raise _map_stat_failure(exc) from exc
    log_file_io(
        _logger, action="stat_artifacts", count=result.count, total_bytes=result.total_bytes
    )
    return result
