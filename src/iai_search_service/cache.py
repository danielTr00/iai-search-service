"""Bounded in-process TTL cache with concurrent-request coalescing."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class BoundedTTLCache(Generic[K, V]):
    def __init__(self, *, ttl_seconds: int, max_entries: int, max_bytes: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._items: OrderedDict[K, tuple[float, V, int]] = OrderedDict()
        self._bytes = 0
        self._inflight: dict[K, asyncio.Task[V]] = {}
        self._waiters: dict[K, int] = {}

    async def get_or_load(
        self,
        key: K,
        loader: Callable[[], Awaitable[V]],
        *,
        cacheable: Callable[[V], bool],
        weight: Callable[[V], int],
    ) -> V:
        if self.ttl_seconds <= 0 or self.max_entries <= 0 or self.max_bytes <= 0:
            return await loader()
        now = time.monotonic()
        item = self._items.get(key)
        if item is not None:
            expiry, value, size = item
            if now < expiry:
                self._items.move_to_end(key)
                return value
            self._bytes -= size
            del self._items[key]
        task = self._inflight.get(key)
        if task is None:
            async def populate() -> V:
                value = await loader()
                if cacheable(value):
                    self._put(key, value, weight(value) + len(repr(key).encode("utf-8")))
                return value

            task = asyncio.create_task(populate())
            self._inflight[key] = task
            self._waiters[key] = 0
        self._waiters[key] += 1
        try:
            return await asyncio.shield(task)
        finally:
            if self._inflight.get(key) is task:
                self._waiters[key] -= 1
                if self._waiters[key] == 0:
                    try:
                        if not task.done():
                            task.cancel()
                        while not task.done():
                            try:
                                await asyncio.shield(task)
                            except asyncio.CancelledError:
                                continue
                            except Exception:
                                break
                        if task.done() and not task.cancelled():
                            try:
                                task.result()
                            except Exception:
                                pass
                    finally:
                        if self._inflight.get(key) is task and self._waiters[key] == 0:
                            self._inflight.pop(key)
                            self._waiters.pop(key)

    def _put(self, key: K, value: V, size: int) -> None:
        if size > self.max_bytes:
            return
        if key in self._items:
            _, _, previous = self._items.pop(key)
            self._bytes -= previous
        self._items[key] = (time.monotonic() + self.ttl_seconds, value, size)
        self._bytes += size
        while len(self._items) > self.max_entries or self._bytes > self.max_bytes:
            _, (_, _, removed) = self._items.popitem(last=False)
            self._bytes -= removed
