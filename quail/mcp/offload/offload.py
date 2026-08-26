"""Run blocking MCP tool bodies off the asyncio event loop."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import anyio.to_thread

T = TypeVar("T")


async def run_blocking(fn: Callable[[], T]) -> T:
    """Await ``fn()`` in a worker thread without abandoning on cancel.

    MCPServer sync tools block the asyncio loop. Call this from ``async def``
    tools so catalog/auth/exec work cannot freeze other MCP clients.
    """

    return await anyio.to_thread.run_sync(fn, abandon_on_cancel=False)
