"""Live canvas render persistence + cache (, low risk).

Takes the rasterized PNG bytes returned by a connected transport's `render_view` and writes them
into the root-scoped live artifacts dir (`<root>/.inkscape-mcp/live/artifacts/`), enforcing the
output-size cap. Returns a WORKSPACE-RELATIVE path (never an absolute host path). No workspace
document is touched, so no Operation Record is required (render is read-only feedback).

the result is cached on the per-session `RenderCache` keyed on
``(doc_revision, viewport, scale)``. ``doc_revision`` is the document-revision digest pulled
cheaply via `get_state_token`, so a cache hit can NEVER serve a stale frame after the document
changes (a changed document yields a new revision digest → a different key). Within the coalescing
latency budget a repeated identical-key request returns the just-cached frame instead of thrashing
the renderer. When the transport cannot supply a revision marker, caching is safely SKIPPED (every
call re-renders) — correctness over speed.
"""

from __future__ import annotations

import contextlib
import secrets
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from inkscape_mcp.config import Settings, get_settings
from inkscape_mcp.live.cache import CacheKey, scale_key, viewport_key
from inkscape_mcp.live.session import LiveSessionManager, get_session_manager
from inkscape_mcp.live.transport import (
    LiveError,
    LiveNotAvailable,
    RenderRegion,
)
from inkscape_mcp.logging_setup import get_logger, log_preview
from inkscape_mcp.workspace import sandbox
from inkscape_mcp.workspace.limits import LimitExceeded, check_output_size

_logger = get_logger("live.render")


def _cache_key(
    manager: LiveSessionManager, region: RenderRegion | None, scale: float | None
) -> CacheKey | None:
    """Build the ``(doc_revision, viewport, scale)`` key, or None when caching can't be safe.

    Pulls the cheap state token for the document-revision digest. If the transport cannot
    serve a state token (no revision marker), returns None so the caller re-renders every time — a
    stale frame must be impossible, so caching is only enabled when a revision is available.
    """
    revision = ""
    with contextlib.suppress(LiveError):
        token, _ = manager.require_transport().get_state_token()
        revision = token.revision
    if not revision:
        return None
    return CacheKey(doc_revision=revision, viewport=viewport_key(region), scale=scale_key(scale))


class LiveRenderResult(BaseModel):
    """Outcome of `live_render_view`: a workspace-relative PNG path + its size."""

    artifact_path: str
    format: str
    size_bytes: int
    region: bool = False
    scale: float | None = None


def _first_root(settings: Settings) -> Path:
    if not settings.workspace_roots:
        raise LiveNotAvailable("no workspace root configured to store the live render")
    return settings.workspace_roots[0]


def render_live_view(
    manager: LiveSessionManager | None = None,
    settings: Settings | None = None,
    region: RenderRegion | None = None,
    scale: float | None = None,
    use_cache: bool = True,
) -> LiveRenderResult:
    """Render the live canvas to a PNG under the live artifacts dir and return its rel path.

        Reads bytes from the connected transport (raising `LiveNotAvailable` if none) and writes them
        atomically (temp + replace) so a partial transfer never leaves a half-written artifact. With
        `region` the renderer clips to that user-unit bbox; with `scale` it downscales/upscales the
        raster. Both must already be server-validated; passing neither renders the whole canvas
        (backward-compatible). View-only — no document mutation, no Operation Record.

    : when ``use_cache`` and a session-scoped `RenderCache` exists, the result is served from /
        stored in a cache keyed on ``(doc_revision, viewport, scale)``. ``doc_revision`` is the
        revision digest, so a hit can never return a stale frame after the document changes. Within the
        coalescing budget a repeated identical-key request returns the just-cached frame instead of
        re-rendering. Caching is skipped (re-render every call) when the transport supplies no revision
        marker — correctness over speed.
    """
    s = settings if settings is not None else get_settings()
    mgr = manager if manager is not None else get_session_manager()
    transport = mgr.require_transport()

    cache = mgr.render_cache() if use_cache else None
    key = _cache_key(mgr, region, scale) if cache is not None else None
    if cache is not None and key is not None:
        # Coalescing first: a same-key request inside the latency budget returns the just-made
        # frame without re-rendering. Falls through to a plain hit (no re-render) past the budget.
        coalesced = cache.within_budget(key)
        if coalesced is not None:
            return coalesced  # type: ignore[no-any-return]
        hit = cache.get(key)
        if hit is not None:
            return hit  # type: ignore[no-any-return]

    png = transport.render_view(region=region, scale=scale)
    root = _first_root(s).resolve()
    sandbox.ensure_live_dirs(root)
    # Random suffix: two renders that land in the same microsecond can't collide on the final name
    # (a collision would leave the cache pointing at an overwritten file → stale bytes).
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    artifacts = sandbox.live_artifacts_dir(root)
    out = artifacts / f"live-view-{stamp}-{secrets.token_hex(4)}.png"
    # Containment guard: refuse to write if the artifacts dir was swapped for a symlink escaping
    # the root between ensure_live_dirs and the write (TOCTOU) — matches the sandbox guard pattern.
    if not out.resolve().parent.is_relative_to(root):
        raise LiveError("live artifacts path escaped the workspace")

    tmp = out.with_name(f"{out.name}.tmp")
    tmp.write_bytes(png)

    # Bound the artifact size BEFORE promoting it to the final name, so an oversized frame is never
    # durably visible at its published path.
    try:
        check_output_size(tmp, s)
    except LimitExceeded:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(out)

    rel = out.relative_to(root).as_posix()
    size_bytes = out.stat().st_size
    log_preview(_logger, doc_id=None, format="png", artifact=rel, live=True)
    result = LiveRenderResult(
        artifact_path=rel,
        format="png",
        size_bytes=size_bytes,
        region=region is not None,
        scale=scale,
    )
    if cache is not None and key is not None:
        cache.put(key, result, size_bytes)
    return result
