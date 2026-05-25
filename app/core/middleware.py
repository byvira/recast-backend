"""Request logging middleware and SlowAPI rate limiter setup."""

import time
import uuid

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logger import logger


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