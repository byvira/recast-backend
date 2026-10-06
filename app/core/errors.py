"""Error responses that always reach the browser as the error they are.

Starlette answers an unhandled exception from its outermost layer, outside CORSMiddleware, so such a response carried no
`Access-Control-Allow-Origin` and the browser reported a CORS failure instead of the real problem. The handlers here add the CORS
headers themselves and give every failure the same body: `detail` (a plain message, what the screens already read), `code` (what
kind of failure it is) and `request_id`, plus `retry_after` for a rate limit.
"""
from __future__ import annotations

import logging
from typing import Iterable

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

logger = logging.getLogger(__name__)

EXPOSED_HEADERS = ["X-Request-ID", "Retry-After"]
DEFAULT_RETRY_AFTER = 30


def cors_headers(request: Request, allowed_origins: Iterable[str]) -> dict[str, str]:
    origin = request.headers.get("origin")
    if origin and origin in set(allowed_origins):
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Expose-Headers": ", ".join(EXPOSED_HEADERS),
            "Vary": "Origin",
        }
    return {}


def _retry_after_seconds(exc: RateLimitExceeded) -> int:
    """How long until the limit that was hit resets, from the limit's own window (never below one second)."""
    try:
        window = int(exc.limit.limit.get_expiry())
        return max(1, window)
    except Exception:  # noqa: BLE001 - a missing detail must not turn a 429 into a 500
        return DEFAULT_RETRY_AFTER


def make_handlers(allowed_origins: Iterable[str]):
    origins = frozenset(allowed_origins)

    async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        retry_after = _retry_after_seconds(exc)
        logger.warning("Rate limit exceeded | PATH=%s | IP=%s", request.url.path, request.client.host if request.client else "unknown")
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Please wait a moment and try again.", "code": "rate_limited", "retry_after": retry_after},
            headers={"Retry-After": str(retry_after), **cors_headers(request, origins)},
        )

    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error | %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Something went wrong on our side. Please try again.", "code": "server_error"},
            headers=cors_headers(request, origins),
        )

    return rate_limit_handler, unhandled_handler
