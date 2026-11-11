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
            content={"detail": "Too many requests. Please wait a moment and try again.", "code": "rate_limited", "retry_after": retry_after,
                     "retryable": True, "retry_after_seconds": retry_after},
            headers={"Retry-After": str(retry_after), **cors_headers(request, origins)},
        )

    async def http_handler(request: Request, exc) -> JSONResponse:
        """The same answer Starlette gives, except that a plain sentence for the member to read is in their language."""
        detail = exc.detail
        reader = getattr(request.state, "reader", None)
        if reader and 400 <= exc.status_code < 500 and not request.url.path.startswith("/api/v1/ops"):
            try:
                from app.shared.reader_language import reader_language, say_error

                detail = await say_error(detail, await reader_language("member", user_id=reader["user_id"], workspace_id=reader["workspace_id"]))
            except Exception:  # noqa: BLE001 - the English message is a correct answer
                detail = exc.detail
        headers = dict(getattr(exc, "headers", None) or {})
        # The same fields on every failure, beside `detail` (which is unchanged, so every screen reads what it always read): a stable code,
        # whether trying again can help, and how long to wait. Tools, the command line and assistants read these instead of guessing.
        from app.core import failures

        named = getattr(exc, "code", None)
        if named == "ai_busy":
            f = failures.ai_busy(int(getattr(exc, "retry_after", 60)))
        else:
            f = failures.from_upstream(exc.status_code, exc.detail)
        extra = {"code": named or f.code, "retryable": f.retryable}
        if f.retry_after_seconds is not None:
            extra["retry_after_seconds"] = f.retry_after_seconds
            headers.setdefault("Retry-After", str(f.retry_after_seconds))
        return JSONResponse(status_code=exc.status_code, content={"detail": detail, **extra}, headers=headers or None)

    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error | %s %s", request.method, request.url.path)
        from app.health import errors

        route = request.scope.get("route")
        errors.capture_exception(
            exc, source="server", where=f"{request.method} {getattr(route, 'path', None) or request.url.path}",
            workspace_id=request.headers.get("x-workspace-id"),
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Something went wrong on our side. Please try again.", "code": "server_error", "retryable": True, "retry_after_seconds": 30},
            headers=cors_headers(request, origins),
        )

    return rate_limit_handler, unhandled_handler, http_handler
