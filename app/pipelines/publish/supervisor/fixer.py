"""
Supervisor auto-fixer.
Attempts to automatically fix FIXABLE content issues
before the supervisor retries the publish.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Platform character limits
PLATFORM_LIMITS = {
    "linkedin":  3000,
    "instagram": 2200,
    "threads":   500,
    "facebook":  63206,
    "reddit":    40000,
    "bluesky":   300,
}


def fix_content(
    platform: str,
    content: str,
    error_message: str,
) -> tuple[bool, str]:
    """
    Attempt to auto-fix content for a platform.
    Returns (fixed, new_content).
    fixed=False means the fix failed and content should be flagged.
    """
    error_lower = error_message.lower()

    # Fix: content too long — truncate at sentence boundary
    if "too long" in error_lower or "character limit" in error_lower:
        limit = PLATFORM_LIMITS.get(platform.lower())
        if limit and len(content) > limit:
            fixed = _truncate_at_sentence(content, limit)
            if fixed:
                logger.info(
                    "Auto-fixed content length for %s: %d → %d chars",
                    platform, len(content), len(fixed),
                )
                return True, fixed
        return False, content

    # Fix: banned hashtag — remove it
    if "banned hashtag" in error_lower or "hashtag not allowed" in error_lower:
        fixed = _remove_hashtags(content)
        logger.info("Auto-fixed hashtags for %s", platform)
        return True, fixed

    # Cannot auto-fix this error type
    return False, content


def _truncate_at_sentence(content: str, limit: int) -> str | None:
    """
    Truncate content at the last sentence boundary before the limit.
    Returns None if no sentence boundary found.
    """
    if len(content) <= limit:
        return content

    # Try to cut at sentence boundary
    truncated = content[:limit]
    last_period = max(
        truncated.rfind('.'),
        truncated.rfind('!'),
        truncated.rfind('?'),
    )
    if last_period > limit * 0.7:  # Only cut if we keep 70%+ of content
        return content[:last_period + 1].strip()

    return None


def _remove_hashtags(content: str) -> str:
    """Strip all hashtags from content."""
    return re.sub(r'#\w+', '', content).strip()