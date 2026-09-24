import asyncio

import pytest

from iai_search_service.cache import BoundedTTLCache


async def test_cache_expires_after_ttl_and_reloads(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("iai_search_service.cache.time.monotonic", lambda: clock[0])
    cache = BoundedTTLCache(ttl_seconds=10, max_entries=2, max_bytes=10)
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        return "value"

    async def get():
        return await cache.get_or_load("key", load, cacheable=lambda _: True, weight=len)

    assert await get() == "value"
    clock[0] += 9
    assert await get() == "value"
    clock[0] += 2
    assert await get() == "value"
    assert calls == 2


async def test_cache_evicts_oldest_entry_and_honors_byte_limit():
    cache = BoundedTTLCache(ttl_seconds=60, max_entries=2, max_bytes=8)
    calls = 0

    async def get(key, value):
        async def load():
            nonlocal calls
            calls += 1
            return value

        return await cache.get_or_load(key, load, cacheable=lambda _: True, weight=len)

    await get("a", "aaa")
    await get("b", "bbb")  # Evicts a by bytes.
    await get("b", "ignored")
    await get("a", "aaa")  # Evicts b.
    await get("oversize", "too large for the bounded cache")
    await get("oversize", "too large for the bounded cache")
    assert calls == 5


async def test_failed_load_is_not_cached_and_concurrent_waiter_survives_cancellation():
    cache = BoundedTTLCache(ttl_seconds=60, max_entries=2, max_bytes=20)
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def load():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        if calls == 1:
            raise ValueError("temporary failure")
        return "success"

    first = asyncio.create_task(cache.get_or_load("key", load, cacheable=lambda _: True, weight=len))
    await started.wait()
    waiter = asyncio.create_task(cache.get_or_load("key", load, cacheable=lambda _: True, weight=len))
    first.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(ValueError, match="temporary failure"):
        await waiter
    assert await cache.get_or_load("key", load, cacheable=lambda _: True, weight=len) == "success"
    assert calls == 2


async def test_orphaned_load_is_cancelled_when_all_waiters_leave():
    cache = BoundedTTLCache(ttl_seconds=60, max_entries=2, max_bytes=20)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def load():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    waiter = asyncio.create_task(cache.get_or_load("key", load, cacheable=lambda _: True, weight=len))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert not cache._inflight


async def test_cache_byte_budget_counts_the_key_as_well_as_the_value():
    cache = BoundedTTLCache(ttl_seconds=60, max_entries=2, max_bytes=10)
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        return "v"

    for _ in range(2):
        await cache.get_or_load("long-source-url", load, cacheable=lambda _: True, weight=len)

    assert calls == 2


async def test_repeated_cancellation_waits_for_loader_cleanup():
    cache = BoundedTTLCache(ttl_seconds=60, max_entries=2, max_bytes=20)
    started = asyncio.Event()
    cleanup = asyncio.Event()
    release = asyncio.Event()

    async def load():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()

    waiter = asyncio.create_task(cache.get_or_load("key", load, cacheable=lambda _: True, weight=len))
    await started.wait()
    waiter.cancel()
    await cleanup.wait()
    waiter.cancel()
    await asyncio.sleep(0)
    assert not waiter.done()
    assert "key" in cache._inflight
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, 1)
    assert not cache._inflight


async def test_failed_cancellation_cleanup_removes_inflight_entry():
    cache = BoundedTTLCache(ttl_seconds=60, max_entries=2, max_bytes=20)
    started = asyncio.Event()

    async def load():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise RuntimeError("cleanup failed")

    waiter = asyncio.create_task(cache.get_or_load("key", load, cacheable=lambda _: True, weight=len))
    await started.wait()
    waiter.cancel()
    with pytest.raises((asyncio.CancelledError, RuntimeError)):
        await waiter
    assert not cache._inflight
    assert not cache._waiters
