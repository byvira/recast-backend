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

_AUTH_MESSAGES = [
    "token expired", "invalid token", "access revoked",
    "permission denied", "unauthorized", "authentication",
    "oauth", "credentials", "forbidden",
]

_FATAL_MESSAGES = [
    "permanently banned", "account suspended", "policy violation",
    "spam", "deprecated", "endpoint removed",
]


def classify_error(
    error_code: int,
    error_message: str,
) -> ErrorType:
    """
    Classify a platform error into one of 4 types.
    Order of precedence: AUTH > FIXABLE > TRANSIENT > FATAL.
    """
    message_lower = error_message.lower()

    # Auth check first — a 403 with "token expired" is AUTH not FATAL
    if error_code in _AUTH_CODES:
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