"""Live Activity Log fan-out.

One Redis pattern subscription per process, shared by every open Activity Log
tab — not one subscription per browser connection, which would multiply Redis
connections by the number of open tabs. Each SSE connection registers an
``asyncio.Queue`` under its workspace; the single reader drops each published
row onto the queues for that workspace, and the SSE handler applies the
caller's visibility before sending anything.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

from app.db.redis import get_redis
from app.shared.activity.store import LIVE_CHANNEL_PREFIX

logger = logging.getLogger(__name__)

#: Per-connection buffer. A client this far behind is dropped rather than
#: letting its queue grow without bound; it refetches on reconnect.
QUEUE_MAX = 200

_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
_reader_task: asyncio.Task | None = None


async def _reader() -> None:
    while True:
        try:
            r = await get_redis()
            pubsub = r.pubsub()
            await pubsub.psubscribe(f"{LIVE_CHANNEL_PREFIX}*")
            async for message in pubsub.listen():
                if message.get("type") != "pmessage":
                    continue
                workspace_id = str(message["channel"])[len(LIVE_CHANNEL_PREFIX):]
                queues = _subscribers.get(workspace_id)
                if not queues:
                    continue
                try:
                    row = json.loads(message["data"])
                except (TypeError, ValueError):
                    continue
                for q in list(queues):
                    try:
                        q.put_nowait(row)
                    except asyncio.QueueFull:
                        # Too far behind — drop it; the SSE handler sees
                        # is_dropped() and closes so the client refetches.
                        queues.discard(q)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("activity live reader error, reconnecting in 2s: %s", exc)
            await asyncio.sleep(2)


def _ensure_reader() -> None:
    global _reader_task
    if _reader_task is None or _reader_task.done():
        _reader_task = asyncio.create_task(_reader())


def subscribe(workspace_id: str) -> asyncio.Queue:
    _ensure_reader()
    q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
    _subscribers[workspace_id].add(q)
    return q


def unsubscribe(workspace_id: str, q: asyncio.Queue) -> None:
    queues = _subscribers.get(workspace_id)
    if queues is not None:
        queues.discard(q)
        if not queues:
            _subscribers.pop(workspace_id, None)


def is_dropped(workspace_id: str, q: asyncio.Queue) -> bool:
    return q not in _subscribers.get(workspace_id, set())


async def stop() -> None:
    global _reader_task
    if _reader_task and not _reader_task.done():
        _reader_task.cancel()
        try:
            await _reader_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _reader_task = None
