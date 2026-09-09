"""
Supervisor retry strategies.
Each error type has a different backoff strategy.
Returns wait time in seconds before next attempt.
"""

from app.pipelines.publish.supervisor.classifier import ErrorType


# Max retry attempts per error type
MAX_RETRIES = {
    ErrorType.TRANSIENT: 3,
    ErrorType.FIXABLE:   1,
    ErrorType.AUTH:      1,
    ErrorType.FATAL:     0,
}

# Backoff sequences in seconds per error type
_BACKOFF = {
    ErrorType.TRANSIENT: [60, 300, 900],      # 1min, 5min, 15min
    ErrorType.FIXABLE:   [0],                  # immediate after fix
    ErrorType.AUTH:      [0],                  # immediate after token refresh
    ErrorType.FATAL:     [],                   # no retries
}


def should_retry(error_type: ErrorType, attempt: int) -> bool:
    """Return True if another retry attempt should be made."""
    return attempt < MAX_RETRIES.get(error_type, 0)


def get_retry_delay(error_type: ErrorType, attempt: int) -> int:
    """
    Return seconds to wait before the next retry.
    attempt is 0-indexed — first retry is attempt 0.
    Returns 0 if no delay needed.
    """
    backoff = _BACKOFF.get(error_type, [])
    if not backoff:
        return 0
    index = min(attempt, len(backoff) - 1)
    return backoff[index]