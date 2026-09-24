"""Cross-instance routing for live text-pipeline sessions.

A streamed run lives on the instance that holds its SSE connection — the
EventEmitter wraps an asyncio.Event on that process's loop, and the pipeline
task runs there. With more than one API instance, a later *resume* or
*status* request can land on a different instance, which used to answer
"lost" for a session that was alive and well next door.

This module closes that gap without moving the session:

* **Heartbeat** — the owning instance refreshes
  ``pipeline_session_owner:{session_id}`` (short TTL) while the session runs,
  so any instance can tell "alive elsewhere" from "died with its instance".
* **Relay** — a resume that arrives on the wrong instance is published on
  ``recast:pipeline:resume``; every instance runs one pattern subscriber and
  the owner applies it to its local emitter. ``PUBLISH``'s receiver count
  tells the caller whether anyone was listening.

What it can't do: if the owning instance crashes, the pipeline's task and
call stack die with it — surviving that needs LangGraph checkpointing
(interrupt()/Command(resume=...)), which is out of scope here. Such a
session is reported as ``lost``, as before.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from typing import Awaitable, Callable, Optional
from uuid import uuid4

from app.db.redis import get_redis

logger = logging.getLogger(__name__)

INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
RESUME_CHANNEL = "recast:pipeline:resume"
HEARTBEAT_TTL_SECONDS = 45
HEARTBEAT_EVERY_SECONDS = 15

_listener_task: Optional[asyncio.Task] = None
_resume_handler: Optional[Callable[[str, str, str], Awaitable[bool]]] = None


def _owner_key(session_id: str) -> str:
    return f"pipeline_session_owner:{session_id}"


async def _beat(session_id: str, workspace_id: str) -> None:
    r = await get_redis()
    await r.set(
        _owner_key(session_id),
        json.dumps({"instance": INSTANCE_ID, "workspace_id": workspace_id}),
        ex=HEARTBEAT_TTL_SECONDS,
    )


async def heartbeat(session_id: str, workspace_id: str) -> None:
    """Run for the lifetime of a session (cancel it when the session ends)."""
    try:
        while True:
            try:
                await _beat(session_id, workspace_id)
            except Exception as exc:  # noqa: BLE001 — a missed beat just ages out
                logger.warning("session heartbeat failed for %s: %s", session_id, exc)
            await asyncio.sleep(HEARTBEAT_EVERY_SECONDS)
    except asyncio.CancelledError:
        try:
            r = await get_redis()
            await r.delete(_owner_key(session_id))
        except Exception:  # noqa: BLE001
            pass
        raise


async def owner(session_id: str) -> Optional[dict]:
    """``{"instance", "workspace_id"}`` of the live owner, or None when no
    instance has beaten recently."""
    try:
        r = await get_redis()
        raw = await r.get(_owner_key(session_id))
        return json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("session owner lookup failed for %s: %s", session_id, exc)
        return None


async def relay_resume(session_id: str, workspace_id: str, choice: str) -> bool:
    """Send a resume to whichever instance owns the session. True when at
    least one instance received it (the owner applies it; others ignore)."""
    r = await get_redis()
    receivers = await r.publish(
        RESUME_CHANNEL,
        json.dumps({"session_id": session_id, "workspace_id": workspace_id, "choice": choice}),
    )
    return receivers > 0


def start_listener(handler: Callable[[str, str, str], Awaitable[bool]]) -> None:
    """Start this instance's one resume subscriber (idempotent). ``handler``
    applies a resume locally and returns whether it owned the session."""
    global _listener_task, _resume_handler
    _resume_handler = handler
    if _listener_task is None or _listener_task.done():
        _listener_task = asyncio.create_task(_listen())


async def _listen() -> None:
    while True:
        try:
            r = await get_redis()
            pubsub = r.pubsub()
            await pubsub.subscribe(RESUME_CHANNEL)
            async for message in pubsub.listen():
                if message.get("type") != "message" or _resume_handler is None:
                    continue
                try:
                    body = json.loads(message["data"])
                    await _resume_handler(body["session_id"], body["workspace_id"], body["choice"])
                except (KeyError, TypeError, ValueError):
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("pipeline resume listener error, reconnecting in 2s: %s", exc)
            await asyncio.sleep(2)


async def stop_listener() -> None:
    global _listener_task
    if _listener_task and not _listener_task.done():
        _listener_task.cancel()
        try:
            await _listener_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _listener_task = None
