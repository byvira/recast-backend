"""Request logging middleware and SlowAPI rate limiter setup."""

import re
import time
import uuid

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

# Exempt by prefix — this path is entirely upload/transform routes, nothing
# JSON-only to keep protected here.
_EXEMPT_PATH_PREFIXES = ("/api/v1/media",)

# Exempt by exact route shape instead of a bare suffix for campaigns — only
# the thumbnail upload (POST /api/v1/campaigns/{id}/thumbnail) is a real
# UploadFile route (same stale-cap bug as /api/v1/media above, own 5MB
# check via MAX_THUMBNAIL_BYTES). The rest of /api/v1/campaigns (create,
# batch-generate, suggest-topics, ...) is JSON and should keep the 2MB
# anti-abuse cap, so this doesn't use a blanket prefix exemption.
#
# A bare string-suffix match (`path.endswith("/thumbnail")`) was too broad:
# any future unrelated route that happens to end in "/thumbnail" (a typo'd
# route name, a settings field) would silently inherit this bypass — the
# exact CORS-masking failure mode this middleware exists to prevent,
# reopened by a naming coincidence. Matched by real shape instead: exactly
# one path segment (the campaign id) between "/api/v1/campaigns/" and
# "/thumbnail", nothing else.
_CAMPAIGN_THUMBNAIL_PATH_RE = re.compile(r"^/api/v1/campaigns/[^/]+/thumbnail$")


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    """Reject requests whose declared Content-Length exceeds the cap.

    Starlette/FastAPI enforce no body size limit by default. This checks the
    Content-Length header up front so an oversized request is rejected before
    it's ever read into memory. Requests without Content-Length (chunked
    transfer) pass through uninspected here — none of this app's clients use
    chunked uploads today.

    Paths under _EXEMPT_PATH_PREFIXES, or exactly matching
    _CAMPAIGN_THUMBNAIL_PATH_RE, skip this check entirely — they have their
    own real, kind-aware size validation downstream, and shouldn't be
    capped by a limit sized for JSON API bodies.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path.startswith(_EXEMPT_PATH_PREFIXES) or _CAMPAIGN_THUMBNAIL_PATH_RE.match(path):
            return await call_next(request)

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_REQUEST_BODY_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Request body too large."},
                    )
            except ValueError:
                pass
        return await call_next(request)


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
limiter = Limiter(key_func=get_remote_address)

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
        query_params = str(request.query_params)

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