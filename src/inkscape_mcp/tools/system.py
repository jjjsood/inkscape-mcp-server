"""System diagnostics tools (E1-03).

Exposes the runtime capability matrix so agents can branch on what the host actually supports.
`list_capabilities` returns a cached probe (probe-once, reuse); `diagnose_runtime` forces a
fresh probe and refreshes that cache. The `inkscape://runtime/capabilities` resource shares the
same cache via `get_cached_capabilities`, so tool and resource always agree for a given probe.

E16-01: the matrix also carries the authoritative MCP tool surface — `tool_count` + `tools`
(name + one-line purpose + risk class) — sourced from the LIVE FastMCP registry
(`mcp.list_tools()`), the same single source of truth `scripts/gen_llms_txt.py` reads. The pure
probe (`inkscape_mcp.runtime.probe`) stays MCP-free; this layer overlays the registry fields with
`with_registry_tools` before tool and resource serve the matrix, so the one count can never drift
from the registered surface.

E16-06: this module also hosts the read-only artifact-stat utilities `stat_artifact` /
`stat_artifacts` — on-disk byte size + sha256 of a sandboxed artifact (and an aggregate for a set)
— so agents read back what they wrote without shelling out to `wc -c` / `sha256sum` / `du -cb`.

All tools here are read-only (Risk class: low) — no Operation Record / snapshot required (ADR-004).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from threading import Lock

from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from inkscape_mcp.config import get_settings
from inkscape_mcp.logging_setup import get_logger, log_file_io
from inkscape_mcp.runtime.probe import Capabilities, ToolInfo, probe_capabilities
from inkscape_mcp.server import mcp
from inkscape_mcp.workspace.limits import LimitExceeded, check_input_size
from inkscape_mcp.workspace.paths import (
    SandboxViolation,
    anchor_to_root,
    owning_root,
    resolve_read_path,
)

_logger = get_logger("tools.system")

#: Chunk size for the streaming sha256 read. Bounds peak memory so hashing a large (but
#: under-limit) artifact never loads the whole file at once (sec.12 memory hygiene).
_HASH_CHUNK_BYTES = 1024 * 1024

# Module-level cache shared with the runtime resource. Guarded by a lock so a concurrent
# re-probe never hands out a half-built matrix.
_cache_lock = Lock()
_cached: Capabilities | None = None

#: The canonical risk vocabulary; matches the derivation in `scripts/gen_llms_txt.py` so the
#: capability map and `llms.txt` report the same risk token for a tool.
_RISK_RE = re.compile(r"Risk class:\s*(?P<risk>low|medium|high|restricted)", re.IGNORECASE)


def _one_line_purpose(description: str | None) -> str:
    """First non-empty docstring line — the tool's one-line purpose (mirrors gen_llms_txt)."""
    for line in (description or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return "(no description)"


def _risk_class(description: str | None) -> str:
    """Parse the `Risk class:` token from a docstring, or `unknown` (mirrors gen_llms_txt)."""
    match = _RISK_RE.search(description or "")
    return match.group("risk").lower() if match else "unknown"


def _registry_tools() -> list[ToolInfo]:
    """Read the LIVE FastMCP tool surface (E16-01): name + one-line purpose + risk class.

    Uses `mcp.list_tools()` — the same async registry accessor `scripts/gen_llms_txt.py` and the
    drift-guard tests read — so the authoritative tool count is one number that cannot drift from
    the registered `@mcp.tool`s. Sorted by name for a stable, deterministic order. Counts only
    tools (not resources or prompts).
    """
    tools = asyncio.run(mcp.list_tools())
    return sorted(
        (
            ToolInfo(
                name=t.name,
                purpose=_one_line_purpose(t.description),
                risk=_risk_class(t.description),
            )
            for t in tools
        ),
        key=lambda t: t.name,
    )


def with_registry_tools(caps: Capabilities) -> Capabilities:
    """Return a copy of `caps` with `tool_count` + `tools` populated from the live registry.

    The pure probe leaves these empty (it has no MCP dependency); this overlay is applied before
    the matrix is served so tool and resource both carry the authoritative surface.
    """
    tool_infos = _registry_tools()
    return caps.model_copy(update={"tools": tool_infos, "tool_count": len(tool_infos)})


def get_cached_capabilities() -> Capabilities:
    """Return the cached capability matrix, probing once on first use.

    Shared by `list_capabilities` and the `inkscape://runtime/capabilities` resource so they
    return identical data for the same probe. The live MCP tool surface (E16-01) is overlaid on
    every read, so a tool registered after the probe is still reflected.
    """
    global _cached
    with _cache_lock:
        if _cached is None:
            _cached = probe_capabilities()
        cached = _cached
    return with_registry_tools(cached)


def refresh_capabilities() -> Capabilities:
    """Run a fresh probe, store it as the cache, and return it (with the live tool surface)."""
    global _cached
    fresh = probe_capabilities()
    with _cache_lock:
        _cached = fresh
    return with_registry_tools(fresh)


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


# --- E16-06: artifact stat (read-only) --------------------------------------


class ArtifactStat(BaseModel):
    """On-disk facts about one sandboxed artifact (E16-06).

    `path` echoes the WORKSPACE-RELATIVE POSIX path of the resolved artifact (never a host path —
    sec.12); `bytes` is its raw on-disk size; `sha256` is the lowercase hex digest of its contents.
    """

    path: str
    bytes: int
    sha256: str


class ArtifactStatSet(BaseModel):
    """Per-file stat plus an aggregate byte total for a SET of artifacts (E16-06).

    `artifacts` carries one `ArtifactStat` per input path (order preserved); `total_bytes` is the
    sum of their `bytes` — the icon-set / dist-tree byte budget without a `du -cb`. `count` is the
    number of artifacts stat'd.
    """

    artifacts: list[ArtifactStat]
    total_bytes: int
    count: int


def _stat_one(raw_path: str) -> ArtifactStat:
    """Resolve `raw_path` through the read-path sandbox and return its size + streamed sha256.

    The path may be workspace-RELATIVE (anchored to the first workspace root, matching
    `open_document` / `save_document_as`) or absolute; either is sandbox-validated and a
    `../`-escape, an absolute path outside the workspace, or a symlink whose target leaves the
    sandbox is rejected with `path rejected: outside workspace` (sec.12). Size is enforced against
    `max_input_bytes` and the digest is computed STREAMING in bounded chunks so a large (but
    under-limit) artifact never loads whole into memory. The returned `path` is workspace-relative
    (no host path leaks). Raises `SandboxViolation` / `LimitExceeded` for the caller to map.
    """
    settings = get_settings()
    anchored = anchor_to_root(raw_path, settings)
    resolved = resolve_read_path(anchored, settings)

    # Enforce the input-size cap BEFORE hashing (sec.12) — never hash an over-limit file.
    size = check_input_size(resolved, settings)

    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)

    return ArtifactStat(
        path=_workspace_relative(resolved, settings.workspace_roots),
        bytes=size,
        sha256=digest.hexdigest(),
    )


def _workspace_relative(resolved: Path, roots: list[Path]) -> str:
    """Return `resolved` as a POSIX path relative to its owning workspace root (no host path).

    The path is already proven contained by `resolve_read_path`, so an owning root always exists;
    the relative form is the only value handed back to the client (sec.12, no host-path leak).
    """
    root = owning_root(resolved, roots)
    if root is None:  # pragma: no cover - resolve_read_path proved containment
        return resolved.name
    return resolved.relative_to(root).as_posix()


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
        stat = _stat_one(path)
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
    stats: list[ArtifactStat] = []
    try:
        for raw in paths:
            stats.append(_stat_one(raw))
    except (SandboxViolation, LimitExceeded) as exc:
        raise _map_stat_failure(exc) from exc
    total = sum(s.bytes for s in stats)
    log_file_io(_logger, action="stat_artifacts", count=len(stats), total_bytes=total)
    return ArtifactStatSet(artifacts=stats, total_bytes=total, count=len(stats))
