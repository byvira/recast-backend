"""Async Redis client dependency and cache helpers."""

from collections.abc import AsyncGenerator
from typing import Any

import redis.asyncio as aioredis

from app.core.config import settings


async def get_redis() -> AsyncGenerator[aioredis.Redis, None]:
    """FastAPI dependency that yields a connected Redis client.

    Opens a connection from the pool on entry and ensures it is closed on exit,
    making it safe to use with ``Depends(get_redis)`` in route handlers.

    Yields:
        Authenticated redis.asyncio.Redis client.
    """
    client: aioredis.Redis = aioredis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
    )
    try:
        yield client
    finally:
        await client.aclose()


async def set_cache(key: str, value: Any, ttl: int = 300) -> None:
    """Serialise *value* to JSON and store it under *key* with a TTL.

    Args:
        key: Redis key string.
        value: JSON-serialisable value to cache.
        ttl: Time-to-live in seconds (default 300).
    """
    # Placeholder: obtain a Redis connection and call SET key value EX ttl
    pass


async def get_cache(key: str) -> Any | None:
    """Retrieve and deserialise the cached value for *key*.

    Args:
        key: Redis key string.

    Returns:
        Deserialised value, or None if the key is absent or expired.
    """
    # Placeholder: obtain a Redis connection and call GET key
    return None
