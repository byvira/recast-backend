"""
Per-platform content validation rules.
Called between generation and publishing to catch anything
the generate node missed or that changed during editing.
"""

import re
from typing import Tuple


def validate_linkedin(content: str) -> Tuple[bool, list[str]]:
    issues = []
    if len(content) > 3000:
        issues.append(f"Content too long: {len(content)} chars (max 3000)")
    if not content.strip():
        issues.append("Content is empty")
    return len(issues) == 0, issues


def validate_instagram(content: str) -> Tuple[bool, list[str]]:
    issues = []
    if len(content) > 2200:
        issues.append(f"Caption too long: {len(content)} chars (max 2200)")
    if not content.strip():
        issues.append("Content is empty")
    return len(issues) == 0, issues


def validate_threads(content: str) -> Tuple[bool, list[str]]:
    issues = []
    if len(content) > 500:
        issues.append(f"Content too long: {len(content)} chars (max 500)")
    if not content.strip():
        issues.append("Content is empty")
    return len(issues) == 0, issues


def validate_facebook(content: str) -> Tuple[bool, list[str]]:
    issues = []
    if len(content) > 63206:
        issues.append(f"Content too long: {len(content)} chars (max 63206)")
    if not content.strip():
        issues.append("Content is empty")
    return len(issues) == 0, issues


def validate_reddit(content: str) -> Tuple[bool, list[str]]:
    issues = []
    if len(content) > 40000:
        issues.append(f"Content too long: {len(content)} chars (max 40000)")
    if not content.strip():
        issues.append("Content is empty")
    return len(issues) == 0, issues


def validate_bluesky(content: str) -> Tuple[bool, list[str]]:
    issues = []
    if len(content) > 300:
        issues.append(f"Content too long: {len(content)} chars (max 300)")
    if not content.strip():
        issues.append("Content is empty")
    return len(issues) == 0, issues


# ─────────────────────────────────────────────────────────────────────────────
# PLATFORM VALIDATOR REGISTRY
# ─────────────────────────────────────────────────────────────────────────────

VALIDATORS = {
    "linkedin":  validate_linkedin,
    "instagram": validate_instagram,
    "threads":   validate_threads,
    "facebook":  validate_facebook,
    "reddit":    validate_reddit,
    "bluesky":   validate_bluesky,
}


def validate_for_platform(
    platform: str,
    content: str,
) -> Tuple[bool, list[str]]:
    """
    Run content validation for a specific platform.
    Returns (is_valid, issues).
    Falls back to no-op validation for unknown platforms.
    """
    validator = VALIDATORS.get(platform.lower())
    if not validator:
        return True, []
    return validator(content)