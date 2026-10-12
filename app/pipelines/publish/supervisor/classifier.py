"""
Supervisor error classifier.
Takes a raw platform error and classifies it into one of 4 types.
The supervisor uses the type to decide what to do next.
"""

from enum import Enum


class ErrorType(str, Enum):
    TRANSIENT = "TRANSIENT"   # retry automatically
    FIXABLE   = "FIXABLE"     # auto-fix content then retry
    AUTH      = "AUTH"        # trigger re-auth flow
    FATAL     = "FATAL"       # flag and alert human


# HTTP status code classifications
_TRANSIENT_CODES  = {429, 500, 502, 503, 504}
_AUTH_CODES       = {401, 403}
_FATAL_CODES      = {400, 404, 422}


# Error message substring classifications
_TRANSIENT_MESSAGES = [
    "rate limit", "timeout", "temporarily unavailable",
    "service unavailable", "try again", "too many requests",
    "network", "connection", "gateway",
]

_FIXABLE_MESSAGES = [
    "too long", "exceeds maximum", "character limit",
    "media required", "image required", "invalid media",
    "unsupported format", "file too large",
    "banned hashtag", "hashtag not allowed",
]

_AUTH_HINTS = (
    "token", "permission", "unauthor", "forbidden", "credential", "oauth", "scope", "access", "expired", "authent", "revoked", "login",
)

_AUTH_MESSAGES = [
    "token has expired", "expiredtoken", "invalid or expired",
    "token expired", "invalid token", "access revoked",
    "permission denied", "unauthorized", "authentication",
    "oauth", "credentials", "forbidden",
]

# A platform's own usage ceiling (YouTube's daily upload quota, an hourly post cap). Platforms answer these with HTTP 403 or 429, but
# signing in again does nothing for them: they pass when the window resets.
_QUOTA_MESSAGES = [
    "quota", "quotaexceeded", "uploadlimitexceeded", "upload limit", "daily limit", "limit exceeded", "limit reached",
]

_FATAL_MESSAGES = [
    "permanently banned", "account suspended", "policy violation",
    "spam", "deprecated", "endpoint removed",
]


#: What a member is told when the platform did not answer in time. The post may already be live, so it is never sent again on its own.
TIMEOUT_NOTE = (
    "The platform didn't answer in time, so we can't tell if the post went out. Check the platform first. "
    "If it is there, choose \"I posted this myself\". If it isn't, send it again yourself."
)


def is_timeout(error_code, error_message: str = "") -> bool:
    text = (error_message or "").lower()
    return error_code == 408 or "timed out" in text or "took too long" in text


def classify_error(
    error_code: int,
    error_message: str,
) -> ErrorType:
    """
    Classify a platform error into one of 4 types.
    Order of precedence: AUTH > FIXABLE > TRANSIENT > FATAL.
    """
    message_lower = error_message.lower()

    # A usage ceiling is never an auth problem, even when the platform answers it with a 403.
    if any(phrase in message_lower for phrase in _QUOTA_MESSAGES):
        return ErrorType.TRANSIENT

    # Auth check first — a 403 with "token expired" is AUTH not FATAL. A 401 always means the sign-in was refused. A 403 only does
    # when it says so (or says nothing): platforms also answer 403 for "this content isn't allowed", and treating that as an
    # expired sign-in sends the member to reconnect an account that is fine.
    if error_code == 401:
        return ErrorType.AUTH
    if error_code == 403 and (not message_lower.strip() or any(hint in message_lower for hint in _AUTH_HINTS)):
        return ErrorType.AUTH
    for phrase in _AUTH_MESSAGES:
        if phrase in message_lower:
            return ErrorType.AUTH

    # Fixable check — content issues we can auto-resolve
    for phrase in _FIXABLE_MESSAGES:
        if phrase in message_lower:
            return ErrorType.FIXABLE

    # Transient check — temporary platform issues
    if error_code in _TRANSIENT_CODES:
        return ErrorType.TRANSIENT
    for phrase in _TRANSIENT_MESSAGES:
        if phrase in message_lower:
            return ErrorType.TRANSIENT

    # Fatal — unknown or permanent errors
    return ErrorType.FATAL


_MEDIA_WORDS = ("media", "image", "picture", "video", "format", "file", "thumbnail")
_POLICY_MESSAGES = ("permanently banned", "account suspended", "policy violation", "spam")

#: What the screens act on. Stable names, so the wording of a platform's message never decides what the member is offered.
FAILURE_CODES = (
    "reconnect_required", "rate_limited", "quota_exceeded", "media_invalid", "content_invalid",
    "platform_unavailable", "policy_blocked", "publish_failed",
)
RETRYABLE_CODES = frozenset({"rate_limited", "quota_exceeded", "platform_unavailable"})


def failure_code(error_type: ErrorType, error_code: int, error_message: str) -> str:
    """The kind of failure, as a stable code the screens can offer the right action for (reconnect, wait, fix a field, try again)."""
    message = (error_message or "").lower()
    if any(phrase in message for phrase in _QUOTA_MESSAGES):
        return "quota_exceeded"
    if error_code == 429 or any(phrase in message for phrase in ("rate limit", "too many requests")):
        return "rate_limited"
    if error_type == ErrorType.AUTH:
        return "reconnect_required"
    if error_type == ErrorType.FIXABLE:
        return "media_invalid" if any(word in message for word in _MEDIA_WORDS) else "content_invalid"
    if error_type == ErrorType.TRANSIENT:
        return "platform_unavailable"
    if any(phrase in message for phrase in _POLICY_MESSAGES):
        return "policy_blocked"
    return "publish_failed"
