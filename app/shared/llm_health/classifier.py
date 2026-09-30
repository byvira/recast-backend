"""Turns a failed model call into one of the failure kinds in catalogue.py.

Order: signals the app raises itself, then provider specific wording, then the HTTP status, then the
exception class. Unknown failures are still `unknown` so nothing is invisible.

Choices worth knowing (spec section 8 overlaps here):
- "per day" wording, or a wait of an hour or more, is `quota_daily` for requests and `quota_tokens` for tokens.
- "per minute" wording is `rate_limit_minute`, for requests and tokens alike.
- A request that is itself larger than the allowance (413 request too large) is `quota_tokens`.
The message patterns are written from the providers' documented and commonly seen wording. They should be
checked against real captured error bodies from Render and adjusted; tests/test_llm_classifier.py holds the
cases, and a case marked `captured` is from a real log."""
from __future__ import annotations

import re

APP_KINDS = {"empty_response", "unparseable_response", "content_blocked", "fallback_failed", "recorder_error"}


def _has(text: str, *needles: str) -> bool:
    return any(n in text for n in needles)


def classify(*, http_status: int | None, error_class: str | None, message: str | None, retry_after_s: float | None = None) -> str:
    cls = (error_class or "").strip()
    if cls in APP_KINDS:
        return cls
    low = (message or "").lower()
    name = cls.lower()
    if retry_after_s is None:
        retry_after_s = retry_after_from_text(message)

    # authentication and access
    if http_status == 401 or _has(low, "invalid api key", "incorrect api key", "api key not valid", "api_key_invalid", "invalid_api_key", "unauthenticated"):
        return "auth_invalid_key"
    if http_status == 402 or (http_status == 403 and _has(low, "billing", "denied access", "permission", "not allowed", "forbidden", "access")) or _has(low, "permission_denied", "permission denied"):
        return "billing_or_access"

    # the model itself
    if http_status == 404 or _has(low, "model_not_found", "model not found", "decommissioned", "no longer available", "is not found for api version"):
        return "model_unavailable"

    # limits (429 and 413)
    if http_status in (429, 413) or _has(low, "rate limit", "rate_limit", "resource_exhausted", "resource exhausted", "quota", "too many requests", "request too large"):
        if _has(low, "tokens per day", "(tpd)", "tokens_per_day", "perday") and _has(low, "token"):
            return "quota_tokens"
        if _has(low, "requests per day", "(rpd)", "per day", "perday", "daily", "requests_per_day") or (retry_after_s is not None and retry_after_s >= 3600):
            return "quota_daily"
        if http_status == 413 or "request too large" in low:
            return "quota_tokens"  # this one request is bigger than the allowance, so waiting will not help
        return "rate_limit_minute"

    # size and format
    if _has(low, "context length", "context_length", "maximum context", "too long", "reduce the length", "exceeds the maximum", "token limit"):
        return "context_too_long"
    if http_status == 400:
        return "bad_request"

    # provider and network
    if http_status == 504 or "timeout" in name or _has(low, "timed out", "timeout"):
        return "timeout"
    if http_status is not None and 500 <= http_status <= 599:
        return "provider_outage"
    if _has(name, "connection", "connect", "network", "ssl") or _has(low, "connection", "dns", "name resolution", "tls", "ssl"):
        return "network_error"
    return "unknown"


_RETRY_AFTER = re.compile(r"try again in\s+(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:([\d.]+)s)?", re.I)


def retry_after_from_text(message: str | None) -> float | None:
    """Groq puts the wait in its message ("Please try again in 7m12s"). Returns seconds, or None."""
    m = _RETRY_AFTER.search(message or "")
    if not m or not any(m.groups()):
        return None
    h, mi, s = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)
