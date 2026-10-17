"""Coordinate retention cleanup with active downloads and cached-video delivery."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from weakref import WeakValueDictionary

_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _key(path: Path) -> str:
    return str(path.absolute())


def busy(path: Path) -> bool:
    lock = _locks.get(_key(path))
    return lock is not None and lock.locked()


@asynccontextmanager
async def guard(path: Path):
    key = _key(path)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    async with lock:
        yield


async def finish_thread(func, *args):
    """Keep a file guard held until its filesystem operation actually finishes."""
    task = asyncio.create_task(asyncio.to_thread(func, *args))
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
            task.exception()
        raise
