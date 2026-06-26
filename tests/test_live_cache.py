"""Live render cache + coalescing tests (E8-06; low risk, no mutation, no Operation Record).

Covers the bounded LRU cache, the revision-keyed freshness guarantee, frame coalescing within an
injected-clock latency budget, fast-downscale vs full-res key distinctness, and that the cache is
reset on disconnect — none of it produces a document mutation or an Operation Record.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from inkscape_mcp.config import ENV_LIVE_ENABLED, ENV_WORKSPACE_ROOTS, Settings, get_settings
from inkscape_mcp.live import session as session_mod
from inkscape_mcp.live.cache import CacheKey, RenderCache, scale_key, viewport_key
from inkscape_mcp.live.records import list_live_operations
from inkscape_mcp.live.render import render_live_view
from inkscape_mcp.live.session import (
    LiveSessionManager,
    get_session_manager,
    reset_session_manager,
)
from inkscape_mcp.live.transport import RenderRegion

from .conftest import FakeTransport

FAKE_PNG = b"\x89PNG\r\n\x1a\nCACHE-FRAME"


# --- Pure cache unit tests --------------------------------------------------


def _k(rev: str = "r0", vp: str = "full", sc: str = "native") -> CacheKey:
    return CacheKey(doc_revision=rev, viewport=vp, scale=sc)


def test_cache_hit_and_miss() -> None:
    cache = RenderCache(max_entries=8, max_bytes=10_000_000)
    assert cache.get(_k()) is None
    cache.put(_k(), "frame-a", size_bytes=100)
    assert cache.get(_k()) == "frame-a"
    # A different revision is a different key → miss.
    assert cache.get(_k(rev="r1")) is None


def test_cache_evicts_past_max_entries() -> None:
    cache = RenderCache(max_entries=2, max_bytes=10_000_000)
    cache.put(_k(rev="a"), "a", 1)
    cache.put(_k(rev="b"), "b", 1)
    cache.put(_k(rev="c"), "c", 1)  # evicts the LRU ("a")
    assert cache.entry_count == 2
    assert cache.get(_k(rev="a")) is None
    assert cache.get(_k(rev="b")) == "b"
    assert cache.get(_k(rev="c")) == "c"


def test_cache_evicts_past_max_bytes() -> None:
    cache = RenderCache(max_entries=100, max_bytes=2 * 1024 * 1024)
    cache.put(_k(rev="a"), "a", 1024 * 1024)
    cache.put(_k(rev="b"), "b", 1024 * 1024)
    cache.put(_k(rev="c"), "c", 1024 * 1024)  # over the 2 MiB budget → drop oldest
    assert cache.total_bytes <= 2 * 1024 * 1024
    assert cache.get(_k(rev="a")) is None
    assert cache.get(_k(rev="c")) == "c"


def test_cache_floors_degenerate_bounds() -> None:
    # A degenerate 0/negative config is floored — the bound can never become "unbounded".
    cache = RenderCache(max_entries=0, max_bytes=-1)
    cache.put(_k(rev="a"), "a", 1)
    cache.put(_k(rev="b"), "b", 1)
    assert cache.entry_count == 1  # floored to a single entry


def test_coalescing_within_budget_uses_clock() -> None:
    now = [0.0]
    cache = RenderCache(
        max_entries=8, max_bytes=10_000_000, coalesce_budget_ms=200, monotonic=lambda: now[0]
    )
    cache.put(_k(), "frame", 10)
    # Inside the budget: coalesced hit.
    now[0] = 0.1
    assert cache.within_budget(_k()) == "frame"
    # Past the budget: no coalesced frame (caller re-renders) — but a plain hit still works.
    now[0] = 0.5
    assert cache.within_budget(_k()) is None
    assert cache.get(_k()) == "frame"


def test_coalescing_disabled_when_budget_zero() -> None:
    cache = RenderCache(max_entries=8, max_bytes=10_000_000, coalesce_budget_ms=0)
    cache.put(_k(), "frame", 10)
    assert cache.within_budget(_k()) is None


def test_viewport_and_scale_keys_distinguish() -> None:
    assert viewport_key(None) == "full"
    assert viewport_key(RenderRegion(x=0, y=0, width=4, height=4)) != "full"
    assert scale_key(None) == "native"
    assert scale_key(0.5) != scale_key(1.0)


# --- Wired into render_live_view (engine, injected manager/settings) --------


def _connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object
) -> tuple[LiveSessionManager, Settings, FakeTransport]:
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    transport = FakeTransport(png=FAKE_PNG)
    settings = Settings(workspace_roots=[tmp_path], live_enabled=True, **overrides)  # type: ignore[arg-type]
    monkeypatch.setattr(session_mod, "select_transport", lambda s, required: transport)
    mgr = LiveSessionManager(settings)
    mgr.connect()
    return mgr, settings, transport


def _spy_render(transport: FakeTransport) -> list[int]:
    """Wrap the transport's render_view to count calls; returns the (mutable) counter list."""
    calls: list[int] = []
    original = transport.render_view

    def counting(region=None, scale=None):  # type: ignore[no-untyped-def]
        calls.append(1)
        return original(region=region, scale=scale)

    transport.render_view = counting  # type: ignore[method-assign]
    return calls


def test_same_key_hits_cache_renders_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No coalescing so we exercise the plain revision-keyed hit path.
    mgr, settings, transport = _connected(tmp_path, monkeypatch, live_coalesce_budget_ms=0.0)
    calls = _spy_render(transport)

    first = render_live_view(manager=mgr, settings=settings)
    second = render_live_view(manager=mgr, settings=settings)

    assert len(calls) == 1  # the second request was a cache hit — no re-render
    assert first.artifact_path == second.artifact_path
    # View-only — no Operation Record from caching.
    assert list_live_operations(settings=settings).count == 0


def test_changed_revision_invalidates_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings, transport = _connected(tmp_path, monkeypatch, live_coalesce_budget_ms=0.0)
    calls = _spy_render(transport)

    render_live_view(manager=mgr, settings=settings)
    # The document changes → the E8-03 revision marker changes → a different cache key → re-render.
    transport.state_revision = "rev-1"
    render_live_view(manager=mgr, settings=settings)

    assert len(calls) == 2  # the stale frame was never served after the revision changed


def test_downscale_vs_full_res_distinct_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings, transport = _connected(tmp_path, monkeypatch, live_coalesce_budget_ms=0.0)
    calls = _spy_render(transport)

    render_live_view(manager=mgr, settings=settings, scale=0.5)  # fast downscale
    render_live_view(manager=mgr, settings=settings, scale=None)  # full-res
    # Different scale → different key → both render (no cross-contamination).
    assert len(calls) == 2
    # ...but a repeat of the downscaled request is a hit.
    render_live_view(manager=mgr, settings=settings, scale=0.5)
    assert len(calls) == 2


def test_coalescing_skips_render_within_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr, settings, transport = _connected(tmp_path, monkeypatch, live_coalesce_budget_ms=500.0)
    calls = _spy_render(transport)

    now = [0.0]
    # Inject the clock into the live cache the session built on connect.
    cache = mgr.render_cache()
    assert cache is not None
    cache._monotonic = lambda: now[0]  # type: ignore[attr-defined]

    render_live_view(manager=mgr, settings=settings)
    now[0] = 0.1  # within the 500 ms budget
    render_live_view(manager=mgr, settings=settings)
    assert len(calls) == 1  # coalesced — no second render

    now[0] = 10.0  # well past the budget; same revision → plain cache hit, still no re-render
    render_live_view(manager=mgr, settings=settings)
    assert len(calls) == 1


def test_use_cache_false_bypasses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mgr, settings, transport = _connected(tmp_path, monkeypatch, live_coalesce_budget_ms=0.0)
    calls = _spy_render(transport)
    render_live_view(manager=mgr, settings=settings, use_cache=False)
    render_live_view(manager=mgr, settings=settings, use_cache=False)
    assert len(calls) == 2  # opt-out → always re-render


def test_no_revision_disables_caching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A transport that cannot serve a state token (no revision marker) can never be cached safely
    # → every call re-renders. Correctness over speed.
    from inkscape_mcp.live.transport import LiveCapabilityUnsupported, LiveStateToken

    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    transport = FakeTransport(png=FAKE_PNG)

    def no_token() -> tuple[LiveStateToken, list[str]]:
        raise LiveCapabilityUnsupported("no state token")

    transport.get_state_token = no_token  # type: ignore[method-assign]
    settings = Settings(
        workspace_roots=[tmp_path], live_enabled=True, live_coalesce_budget_ms=500.0
    )
    monkeypatch.setattr(session_mod, "select_transport", lambda s, required: transport)
    mgr = LiveSessionManager(settings)
    mgr.connect()

    calls = _spy_render(transport)
    render_live_view(manager=mgr, settings=settings)
    render_live_view(manager=mgr, settings=settings)
    assert len(calls) == 2


# --- Session lifecycle (global singleton) -----------------------------------


@pytest.fixture
def live_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_LIVE_ENABLED, "1")
    monkeypatch.setenv(ENV_WORKSPACE_ROOTS, str(tmp_path))
    get_settings.cache_clear()
    reset_session_manager()
    monkeypatch.setattr(session_mod, "probe_transports", lambda settings=None: [])
    monkeypatch.setattr(
        session_mod, "select_transport", lambda s, required: FakeTransport(png=FAKE_PNG)
    )


def test_cache_built_on_connect_reset_on_disconnect(live_on: None) -> None:
    mgr = get_session_manager()
    assert mgr.render_cache() is None  # nothing before connect
    mgr.connect()
    cache = mgr.render_cache()
    assert cache is not None
    cache.put(_k(), "frame", 10)
    assert cache.entry_count == 1
    mgr.disconnect()
    # Reset on disconnect → a frame from one session can never leak into the next.
    assert mgr.render_cache() is None


def test_reconnect_gives_fresh_cache(live_on: None) -> None:
    mgr = get_session_manager()
    mgr.connect()
    first = mgr.render_cache()
    assert first is not None
    first.put(_k(), "stale", 10)
    mgr.disconnect()
    mgr.connect()
    second = mgr.render_cache()
    assert second is not None
    assert second is not first
    assert second.get(_k()) is None
