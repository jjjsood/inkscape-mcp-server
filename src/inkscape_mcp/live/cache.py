"""Bounded render-frame cache for the live loop (low risk).

Keeps the perceive→act→observe loop low-latency under rapid edits WITHOUT changing what a frame
is or touching the document. Pure server-side bookkeeping:

* **Render cache** — keyed on ``(doc_revision, viewport, scale)``. A hit returns the previously
  written frame's `LiveRenderResult` and SKIPS the re-render. ``doc_revision`` is the
  document-revision digest (`LiveStateToken.revision`), so the cache invalidates the instant the
  document changes: a stale frame can NEVER be served after an edit, because a changed document
  yields a different revision digest and therefore a different key. The cache is bounded both by
  entry count AND total bytes (LRU eviction, oldest-touched first); the floors live in `Settings`.

READ-ONLY: the cache records nothing in the document and produces NO Operation Record. It holds
only server-minted, workspace-relative artifact paths — never a host path or raw markup. The cache
lives on the `LiveSessionManager` and is reset on every connect/disconnect, so a frame from one
session can never leak into another.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from inkscape_mcp.live.transport import RenderRegion

#: Floor on the cache's max-entries bound — a degenerate ``0``/negative config can never disable the
#: bound into "unbounded" growth; it is clamped up to this minimum.
MIN_CACHE_MAX_ENTRIES = 1

#: Floor on the cache's max-bytes bound (1 MiB) — likewise clamped so a misconfigured tiny/negative
#: value cannot turn the byte budget into "unbounded".
MIN_CACHE_MAX_BYTES = 1 * 1024 * 1024


def viewport_key(region: RenderRegion | None) -> str:
    """Stable cache-key fragment for a render viewport (whole canvas, or a clipped region).

    ``None`` (whole canvas) maps to a fixed marker; a region maps to its rounded bbox so two
    numerically-equal regions share a key. View-only — derives nothing from the document.
    """
    if region is None:
        return "full"
    return "|".join(repr(round(v, 4)) for v in (region.x, region.y, region.width, region.height))


def scale_key(scale: float | None) -> str:
    """Stable cache-key fragment for a render scale (``None`` = native 1:1)."""
    if scale is None:
        return "native"
    return repr(round(float(scale), 6))


@dataclass(frozen=True)
class CacheKey:
    """Render-cache key: the document revision plus the viewport + scale that produced the frame.

    ``doc_revision`` is the `LiveStateToken.revision` digest. Because it is part of the key, a
    document change (new revision digest) yields a DIFFERENT key, so the previous frame is never
    returned for the changed document — the freshness guarantee.
    """

    doc_revision: str
    viewport: str
    scale: str


@dataclass
class _Entry:
    """One cached frame: its result payload and byte size."""

    value: Any
    size_bytes: int


class RenderCache:
    """Bounded LRU render-frame cache.

    Bounded by BOTH ``max_entries`` and ``max_bytes`` (whichever binds first); eviction drops the
    least-recently-used entry until both bounds hold. Not internally locked: the holding
    `LiveSessionManager` already serializes live access, and a render is single-flight.
    """

    def __init__(self, max_entries: int, max_bytes: int) -> None:
        self._max_entries = max(MIN_CACHE_MAX_ENTRIES, int(max_entries))
        self._max_bytes = max(MIN_CACHE_MAX_BYTES, int(max_bytes))
        self._entries: OrderedDict[CacheKey, _Entry] = OrderedDict()
        self._total_bytes = 0

    # --- queries -------------------------------------------------------------

    def get(self, key: CacheKey) -> Any | None:
        """Return the cached frame for ``key`` (marking it most-recently-used), or ``None``."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry.value

    # --- mutation ------------------------------------------------------------

    def put(self, key: CacheKey, value: Any, size_bytes: int) -> None:
        """Insert/replace a frame under ``key`` and evict LRU until both bounds hold."""
        existing = self._entries.pop(key, None)
        if existing is not None:
            self._total_bytes -= existing.size_bytes
        size = max(0, int(size_bytes))
        self._entries[key] = _Entry(value=value, size_bytes=size)
        self._total_bytes += size
        self._evict()

    def _evict(self) -> None:
        """Drop least-recently-used entries until both the count and byte bounds are satisfied.

        A single oversized entry (larger than the whole byte budget on its own) is retained — it is
        the only entry — so the cache never spins evicting the item it just stored.
        """
        while len(self._entries) > self._max_entries or (
            self._total_bytes > self._max_bytes and len(self._entries) > 1
        ):
            _, evicted = self._entries.popitem(last=False)
            self._total_bytes -= evicted.size_bytes

    def clear(self) -> None:
        """Drop every cached frame (called on connect/disconnect)."""
        self._entries.clear()
        self._total_bytes = 0

    # --- introspection (tests / logging) ------------------------------------

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes
