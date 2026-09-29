"""One log line per support request: who, what, which ticket, how long.

Added as a router dependency on every support router. It logs the route
template (for example ``/tickets/{ticket_id}/messages``), never the query
string or the body, so no message text, search term or file name can reach the
logs. The request-wide line (status and latency) is written by
``RequestLoggingMiddleware``; this adds the parts it cannot know: the ticket,
the acting user and the outcome.
"""

from __future__ import annotations

import logging
import time
from typing import AsyncIterator

from fastapi import Depends, Request

from app.core.auth import get_current_user

logger = logging.getLogger("app.support")


async def support_request_log(request: Request, user: dict = Depends(get_current_user)) -> AsyncIterator[None]:
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except Exception as exc:
        # An error the route raised on purpose (404, 409, 429...) or a crash.
        outcome = f"error:{getattr(exc, 'status_code', 500)}"
        raise
    finally:
        route = request.scope.get("route")
        template = getattr(route, "path", request.url.path)
        params = request.scope.get("path_params") or {}
        logger.info(
            "support_request method=%s route=%s ticket=%s actor=%s outcome=%s latency_ms=%.1f",
            request.method,
            template,
            params.get("ticket_id") or "-",
            user.get("id", "-"),
            outcome,
            (time.perf_counter() - started) * 1000,
        )
