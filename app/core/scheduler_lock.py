"""Redis-backed lock so APScheduler jobs don't double-fire across instances.

``AsyncIOScheduler`` (see ``app/main.py``) runs in-process. With more than one
web instance — or one restarting mid-tick — every scheduled job would
otherwise fire once per instance. Each job acquires a short-lived Redis lock
before running and releases it as soon as it finishes; if Redis is
unreachable the job still runs (fail-open — a missed lock is a smaller risk
than a scheduled job silently never running).
"""

from __future__ import annotations

import functools
import logging
import uuid
from typing import Awaitable, Callable, TypeVar

from app.db.redis import get_redis

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Awaitable[None]])


def distributed_job_lock(name: str, ttl_seconds: int) -> Callable[[F], F]:
    """Skip this call if another instance already holds the ``name`` lock.

    The lock is released the moment the job finishes (success or failure) so
    the *next* scheduled tick is never blocked by it — ``ttl_seconds`` only
    guards against the lock being stuck forever if the process dies mid-job.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            lock_key = f"lock:scheduler:{name}"
            try:
                redis = await get_redis()
                token = uuid.uuid4().hex
                acquired = await redis.set(lock_key, token, nx=True, ex=ttl_seconds)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "scheduler lock: Redis unavailable for %s, running unlocked: %s",
                    name, exc,
                )
                return await func(*args, **kwargs)

            if not acquired:
                logger.info(
                    "scheduler lock: %s already running on another instance, skipping this tick",
                    name,
                )
                return None

            try:
                return await func(*args, **kwargs)
            finally:
                try:
                    # only the holder lets go: a job that ran past its lock time must not delete the lock another instance now holds
                    held = await redis.get(lock_key)
                    if (held.decode() if isinstance(held, bytes) else held) == token:
                        await redis.delete(lock_key)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("scheduler lock: failed to release %s: %s", name, exc)

        return wrapper  # type: ignore[return-value]

    return decorator
