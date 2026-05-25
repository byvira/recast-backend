"""Async Redis client — module-level singleton connection pool."""

from typing import Any

import redis.asyncio as aioredis

from app.core.config import settings

_redis_client: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Return the singleton Redis client, creating it on first access.

    The underlying redis.asyncio client manages a connection pool internally,
    so this singleton is safe to share across concurrent async tasks.

    Returns:
        Configured aioredis.Redis client with decode_responses=True.
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis_client


async def close_redis() -> None:
    """Close the Redis connection pool on application shutdown."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


async def set_cache(key: str, value: Any, ttl: int = 300) -> None:
    """Serialise *value* to JSON and store it under *key* with a TTL.

    Args:
        key: Redis key string.
        value: JSON-serialisable value to cache.
        ttl: Time-to-live in seconds (default 300).
    """
    import json

    client = await get_redis()
    await client.set(key, json.dumps(value), ex=ttl)


async def get_cache(key: str) -> Any | None:
    """Retrieve and deserialise the cached value for *key*.

    Args:
        key: Redis key string.

    Returns:
        Deserialised value, or None if the key is absent or expired.
    """
    import json

    client = await get_redis()
    raw = await client.get(key)
    if raw is None:
        return None
    return json.loads(raw)
