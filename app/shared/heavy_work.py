"""One gate for the work that uses a lot of memory or processor time: decoding long audio, picture exports and ZIP builds,
background removal, ffmpeg.

The server has 512 MB and one process. Each workspace is already limited to three running jobs, but ten workspaces times three
jobs is thirty heavy jobs at once. Everything heavy goes through `run_heavy`, which lets only a few run together; the rest wait
their turn (and can still be cancelled while they wait), and the work runs in a thread so the server keeps answering meanwhile.

Stopping heavy work. A thread cannot be killed, so a cancel works with the function: when the task waiting on `run_heavy` is cancelled,
a stop flag is raised, and a function that loops (a page of a PDF, a file of a ZIP, a layer of a picture) calls `stop_point()` between
steps and ends at once. The slot is kept until the thread has really ended (or STOP_WAIT_SECONDS have passed), so a cancelled job
never leaves a second heavy job running beside the one that is still winding down. A function with no stop point simply finishes its
current step, which is the same as before, but now holds its slot until it does."""
from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from typing import Any, Callable, TypeVar

from app.core import limits

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: How many heavy jobs run at the same time. One keeps the memory predictable; raise it with the instance size.
HEAVY_CONCURRENCY = limits.HEAVY_WORK_CONCURRENCY
#: After a cancel, how long the slot is held for the thread to reach a stop point and end.
STOP_WAIT_SECONDS = 20.0

_gate: asyncio.Semaphore | None = None
_stop: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar("heavy_stop", default=None)


class HeavyStopped(Exception):
    """Raised by `stop_point()` inside heavy work when the job it belongs to was cancelled."""


def stop_point() -> None:
    """Call between the steps of heavy work that loops. Does nothing unless the job was cancelled, in which case the work ends here."""
    flag = _stop.get()
    if flag is not None and flag.is_set():
        raise HeavyStopped()


def _get_gate() -> asyncio.Semaphore:
    global _gate
    if _gate is None:
        _gate = asyncio.Semaphore(max(1, HEAVY_CONCURRENCY))
    return _gate


def heavy_slot() -> asyncio.Semaphore:
    """The gate itself, for heavy work that is not a plain function (a subprocess such as ffmpeg): `async with heavy_slot():`."""
    return _get_gate()


async def run_heavy(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run the blocking function `fn` in a thread, after waiting for a free slot."""
    async with _get_gate():
        flag = threading.Event()
        token = _stop.set(flag)
        try:
            # the thread is started inside this context, so it sees the flag
            work = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
        finally:
            _stop.reset(token)
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            flag.set()
            # Keep the slot until the thread has ended, so a cancelled job does not leave heavy work running beside the next one.
            done, _ = await asyncio.wait({work}, timeout=STOP_WAIT_SECONDS)
            if not done:
                logger.warning("Heavy work %s did not stop within %ss after a cancel; its slot is released and it will end on its own", getattr(fn, "__name__", fn), STOP_WAIT_SECONDS)
            elif not work.cancelled():
                # nobody is waiting for the result or the error any more
                work.exception()
            raise
