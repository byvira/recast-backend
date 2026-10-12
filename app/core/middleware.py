"""Request logging middleware and SlowAPI rate limiter setup."""

import re
import time
import uuid
from typing import Iterable

import structlog
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.logger import logger

# Sane ceiling for a JSON API request body — none of the *JSON* routes need
# more than a couple of MB, so this stays a real anti-abuse cap for them.
#
# Real file uploads (POST /api/v1/media — images/video/audio, added this
# session) are the one deliberate exception, see _EXEMPT_PATH_PREFIXES below.
# That endpoint has its own real per-workspace, per-media-kind size check
# (app.api.v1.media._max_bytes_for, up to 200MB for video) — this outer cap
# staying at 2MB for it was a stale assumption from before that endpoint
# existed (the comment here used to say "none of this app's routes accept
# file uploads," which was true then, not now), and it silently 413'd every
# real upload over 2MB. Worse: because this middleware is the *outermost*
# layer (runs before CORSMiddleware — see main.py), that 413 never carried
# CORS headers, so the browser reported it as a CORS failure instead of a
# clear size-limit error — confirmed live against the deployed backend.
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024  # 2 MB

_MB = 1024 * 1024

# Real file-upload routes get their own, larger ceiling instead of the JSON cap. The ceiling is the biggest file the route can
# ever accept (the workspace schema maxima: image 25 MB, audio 100 MB, video 200 MB, support attachments and videos 10 and
# 50 MB, thumbnails 5 MB, campaign source files 10 MB) plus room for the form around it. Each route still checks its own,
# lower, per-workspace limit while it reads. Paths are matched exactly (or by the one shape below), never by suffix, so a
# lookalike route does not inherit the bigger ceiling.
_UPLOAD_PREFIX_LIMITS: tuple[tuple[str, int], ...] = (("/api/v1/media", 210 * _MB),)
_UPLOAD_PATH_LIMITS: dict[str, int] = {
    "/api/v1/image-assets/upload": 30 * _MB,
    "/api/v1/audio-assets/upload": 110 * _MB,
    "/api/v1/audio-assets/upload/background": 110 * _MB,
    "/api/v1/thumbnails/upload": 6 * _MB,
    "/api/v1/support/uploads": 55 * _MB,
    "/api/v1/ops/support/uploads": 55 * _MB,
    "/api/v1/campaigns/source-file": 11 * _MB,
}
_CAMPAIGN_THUMBNAIL_PATH_RE = re.compile(r"^/api/v1/campaigns/[^/]+/thumbnail$")
_CAMPAIGN_THUMBNAIL_LIMIT = 6 * _MB


def body_limit_for(path: str) -> int:
    """The largest request body accepted on `path`."""
    if path in _UPLOAD_PATH_LIMITS:
        return _UPLOAD_PATH_LIMITS[path]
    for prefix, limit in _UPLOAD_PREFIX_LIMITS:
        if path.startswith(prefix):
            return limit
    if _CAMPAIGN_THUMBNAIL_PATH_RE.match(path):
        return _CAMPAIGN_THUMBNAIL_LIMIT
    return MAX_REQUEST_BODY_BYTES


class _BodyTooLarge(Exception):
    """Raised from inside the receive channel when a body with no declared length goes past its ceiling."""


class MaxBodySizeMiddleware:
    """Refuse a request body larger than its route's ceiling (see `body_limit_for`).

    Two checks: the declared Content-Length is looked at first, so an oversize request is refused before anything is read; and
    the bytes actually received are counted, so a body sent in chunks with no Content-Length (or one that lies about its
    length) is stopped at the ceiling too instead of being read whole into memory.

    This sits outside the CORS layer, so a rejection adds the CORS headers itself; otherwise the browser reports a CORS error
    and hides the real reason ("request too large").
    """

    def __init__(self, app, allowed_origins: Iterable[str] = ()) -> None:
        self.app = app
        self._allowed_origins = frozenset(allowed_origins)

    def _cors_headers(self, headers: dict[bytes, bytes]) -> list[tuple[bytes, bytes]]:
        origin = headers.get(b"origin", b"").decode("latin-1")
        if origin and origin in self._allowed_origins:
            return [
                (b"access-control-allow-origin", origin.encode("latin-1")),
                (b"access-control-allow-credentials", b"true"),
                (b"vary", b"Origin"),
            ]
        return []

    async def _reject(self, scope, send) -> None:
        headers = dict(scope.get("headers") or [])
        body = b'{"detail":"Request body too large."}'
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
            + self._cors_headers(headers),
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = body_limit_for(scope["path"])
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    await self._reject(scope, send)
                    return
            except ValueError:
                pass

        received = 0
        started = False

        async def counted_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    raise _BodyTooLarge()
            return message

        async def tracked_send(message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, tracked_send)
        except _BodyTooLarge:
            if started:
                raise
            await self._reject(scope, send)


# ──────────────────────────────────────────────────────────────────────────────
# Rate Limiter
# ──────────────────────────────────────────────────────────────────────────────

# Shared limiter instance used across route decorators.
#
# Example:
# @limiter.limit("10/minute")
#
# Registered in main.py:
# app.state.limiter = limiter
#
def client_key(request: Request) -> str:
    """Who a rate limit counts. Behind the host's proxy the last entry of X-Forwarded-For is the address that proxy saw; the first
    entries are whatever the caller chose to send, so trusting them would give every request a new bucket. Without the header (a
    direct connection, tests) it is the connecting address."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return get_remote_address(request)


limiter = Limiter(key_func=client_key)

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log all incoming requests with request tracing and latency."""

    async def dispatch(
        self,
        request: Request,
        call_next,
    ) -> Response:
        request_id = str(uuid.uuid4())[:8]
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        method = request.method
        path = request.url.path
        # Support pages take search terms (member names, emails) in the query
        # string, so those are never written to the logs.
        query_params = (
            "" if path.startswith(("/api/v1/support", "/api/v1/ops/support")) else str(request.query_params)
        )

        client_ip = (
            request.client.host
            if request.client
            else "unknown"
        )

        start_time = time.perf_counter()

        try:
            response = await call_next(request)

            process_time = (
                time.perf_counter() - start_time
            ) * 1000

            status_code = response.status_code

            logger.info(
                (
                    "[%s] %s %s%s | "
                    "STATUS=%s | "
                    "TIME=%.2fms | "
                    "IP=%s"
                ),
                request_id,
                method,
                path,
                f"?{query_params}" if query_params else "",
                status_code,
                process_time,
                client_ip,
            )

            response.headers["X-Request-ID"] = request_id

            return response

        except Exception as exc:
            process_time = (
                time.perf_counter() - start_time
            ) * 1000

            logger.exception(
                (
                    "[%s] %s %s | "
                    "FAILED | "
                    "TIME=%.2fms | "
                    "IP=%s | "
                    "ERROR=%s"
                ),
                request_id,
                method,
                path,
                process_time,
                client_ip,
                str(exc),
            )

            raise