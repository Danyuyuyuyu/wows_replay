"""Stable data-service boundary used by rendering and translation consumers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DataService:
    """Expose data operations without coupling callers to updater internals."""

    backend: Any

    async def upstream_has(self, version: str, build: str) -> bool:
        return await self.backend.upstream_has(version, build)

    def install_in_cooldown(self, version: str, build: str) -> bool:
        return self.backend.install_in_cooldown(version, build)

    async def install(self, version: str, build: str) -> bool:
        return await self.backend.install_data(version, build)

    async def await_thread(self, func, *args):
        return await self.backend.await_thread(func, *args)

    async def run_git_operation(self, func, *args):
        return await self.backend.run_git_operation(func, *args)

    def proxy(self) -> str | None:
        return self.backend.proxy()

    @asynccontextmanager
    async def in_use(self):
        async with self.backend.data_in_use():
            yield

    @asynccontextmanager
    async def source_lock(self):
        async with self.backend.data_source_lock():
            yield
