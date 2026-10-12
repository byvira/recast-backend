"""One gate for the work that uses a lot of memory or processor time: decoding long audio, picture exports and ZIP builds,
background removal, ffmpeg.

The server has 512 MB and one process. Each workspace is already limited to three running jobs, but ten workspaces times three
jobs is thirty heavy jobs at once. Everything heavy goes through `run_heavy`, which lets only a few run together; the rest wait
their turn (and can still be cancelled while they wait), and the work runs in a thread so the server keeps answering meanwhile.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Callable, TypeVar

from app.core import limits

T = TypeVar("T")

#: How many heavy jobs run at the same time. One keeps the memory predictable; raise it with the instance size.
HEAVY_CONCURRENCY = limits.HEAVY_WORK_CONCURRENCY

_gate: asyncio.Semaphore | None = None


def _get_gate() -> asyncio.Semaphore:
    global _gate
    if _gate is None:
        _gate = asyncio.Semaphore(max(1, HEAVY_CONCURRENCY))
    return _gate


async def run_heavy(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run the blocking function `fn` in a thread, after waiting for a free slot."""
    async with _get_gate():
        return await asyncio.to_thread(fn, *args, **kwargs)
