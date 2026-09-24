"""Cancellation-safe offloading of extraction work."""

import asyncio
from collections.abc import Callable


async def run_extraction(function: Callable[..., str | None], *args: object, **kwargs: object) -> str | None:
    """Do not release a request slot before its worker thread has stopped."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
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
        raise
