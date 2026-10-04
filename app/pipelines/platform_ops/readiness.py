"""The go-live checklist for a platform, worked out from the code and the saved settings.

Nothing here is ticked by hand except the two facts only a person can confirm: that a live test was run, and that the
registry's facts were checked against the platform's own documentation. Every other check is computed when asked.

Each check is {key, label, state, source, detail}:
    state   "pass", "fail", "na" (does not apply to this kind of platform) or "needs_human"
    source  "code", "config" or "human"
Which checks can block going live depends on how the platform connects (its integration pattern).
"""

from __future__ import annotations

from typing import Any, Optional

from app.pipelines.publish.generic.adapter import ADAPTER_PATTERNS
from app.pipelines.publish.generic.manual_handoff_publisher import template_problem
from app.platforms.base import PlatformDefinition

# The checks that stop a platform going live, per integration pattern.
BLOCKING: dict[str, tuple[str, ...]] = {
    "api_publish": ("publisher", "validator", "live_test", "facts", "rollout"),
    "token_webhook": ("publisher", "validator", "config", "live_test", "facts", "rollout"),
    "manual_handoff": ("publisher", "validator", "config", "live_test", "facts", "rollout"),
    "rss_pull": ("config", "facts", "rollout"),
    "generation_only": (),
}


def _check(key: str, label: str, state: str, source: str, detail: str) -> dict[str, str]:
    return {"key": key, "label": label, "state": state, "source": source, "detail": detail}


def _config_check(definition: PlatformDefinition, config: Optional[dict[str, Any]]) -> dict[str, str]:
    pattern = definition.integration_pattern
    fields = (config or {}).get("fields") or {}
    if pattern in ("api_publish", "generation_only"):
        return _check("config", "Settings saved", "na", "config", "Set in code, nothing to enter here.")
    if config is None:
        return _check("config", "Settings saved", "fail", "config", "Nothing has been saved for this platform yet.")
    if not config.get("enabled", True):
        return _check("config", "Settings saved", "fail", "config", "The saved settings are switched off.")
    if pattern == "manual_handoff":
        problem = template_problem(str(fields.get("compose_url_template") or ""))
        if problem:
            return _check("config", "Settings saved", "fail", "config", problem)
    if pattern == "rss_pull" and not str(fields.get("submission_url") or "").strip():
        return _check("config", "Settings saved", "fail", "config", "The submission address is missing.")
    return _check("config", "Settings saved", "pass", "config", "Required settings are saved.")


def build_readiness(
    definition: PlatformDefinition,
    ops: dict[str, Any],
    config: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """`ops` is the platform_ops record (or its derived default). `config` is the platform-wide settings row in its
    public form (no secrets), or None when nothing is saved."""
    pattern = definition.integration_pattern
    adapter_pattern = pattern in ADAPTER_PATTERNS

    if definition.publisher_cls:
        publisher = _check("publisher", "Publisher present", "pass", "code", "A publisher is registered in code.")
    elif adapter_pattern:
        publisher = _check("publisher", "Publisher present", "pass", "code", "The generic publisher handles this pattern.")
    elif pattern == "api_publish":
        publisher = _check("publisher", "Publisher present", "fail", "code", "No publisher has been written yet.")
    else:
        publisher = _check("publisher", "Publisher present", "na", "code", "This platform has no publisher.")

    if definition.validator_fn:
        validator = _check("validator", "Validator present", "pass", "code", "A content validator is registered.")
    elif adapter_pattern:
        validator = _check("validator", "Validator present", "pass", "code", "The generic length and empty-post checks apply.")
    elif pattern == "api_publish":
        validator = _check("validator", "Validator present", "fail", "code", "No content validator is registered.")
    else:
        validator = _check("validator", "Validator present", "na", "code", "Nothing is validated before posting.")

    analytics = (
        _check("analytics", "Analytics fetcher", "pass", "code", "Results can be read from this platform.")
        if definition.analytics_fetcher_cls
        else _check("analytics", "Analytics fetcher", "na", "code", "No results are read from this platform.")
    )

    auth_test = ops.get("auth_test")
    if pattern in ("rss_pull", "generation_only"):
        live_test = _check("live_test", "Live test passed", "na", "human", "There is nothing to post to.")
    elif auth_test:
        who = auth_test.get("tested_by") or "someone"
        live_test = _check("live_test", "Live test passed", "pass", "human", f"Recorded by {who}.")
    else:
        live_test = _check("live_test", "Live test passed", "needs_human", "human", "No live test has been recorded.")

    if definition.confidence != "unverified":
        facts = _check("facts", "Registry facts verified", "pass", "code", f"Confidence is {definition.confidence}.")
    elif ops.get("facts_verified"):
        facts = _check("facts", "Registry facts verified", "pass", "human", "Checked against the platform's own documentation.")
    else:
        facts = _check("facts", "Registry facts verified", "needs_human", "human", "The registry facts are unverified.")

    rollout = _check("rollout", "Rollout chosen", "pass", "config", f"Starts as: {ops['rollout']['scope'].replace('_', ' ')}.")

    checks = [publisher, validator, analytics, _config_check(definition, config), live_test, facts, rollout]
    blocking = BLOCKING.get(pattern, ())
    blockers = [c["key"] for c in checks if c["key"] in blocking and c["state"] in ("fail", "needs_human")]
    return {
        "checks": checks,
        "can_go_live": bool(blocking) and not blockers,
        "blockers": blockers,
        "waiting_for_developer": pattern == "api_publish" and not definition.publisher_cls,
    }
