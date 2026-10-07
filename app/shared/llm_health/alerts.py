"""Alerts for LLM problems: email always available, Slack when a webhook is set. Deciding what to send is
pure (`decide`); sending is separate and never raises, because an alert failing must not affect anything else.

Each alert has a key. The same key is not sent again within its cooldown, so one storm is one message."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.shared.llm_health import issues

logger = logging.getLogger(__name__)

DEFAULT_RULES: dict[str, Any] = {
    "email_to": [],            # addresses; empty means email is off until an admin adds one
    "slack": True,             # used only when SLACK_WEBHOOK_URL is set
    "cooldown_minutes": 60,
    "new_issue_min_priority": "high",   # critical or high by default
    "quota_warning": True,
    "quota_critical": True,
    "reopened": True,
    "provider_down": True,
}
PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class Alert:
    key: str
    kind: str          # new_issue | reopened | quota_warning | quota_critical | provider_down
    severity: str
    subject: str
    body: str


def _wants(priority: str, minimum: str) -> bool:
    return PRIORITY_RANK.get(priority, 9) <= PRIORITY_RANK.get(minimum, 1)


def decide(
    *, rules: dict[str, Any], created: list[dict[str, Any]], reopened: list[dict[str, Any]], quota: list[dict[str, Any]],
    providers: dict[str, str], recently_sent: dict[str, datetime], now: datetime, base_url: str = "",
) -> list[Alert]:
    """Which alerts to send now. `recently_sent` maps alert keys to when they were last sent."""
    rules = {**DEFAULT_RULES, **(rules or {})}
    cooldown = timedelta(minutes=int(rules["cooldown_minutes"]))
    out: list[Alert] = []

    def add(alert: Alert) -> None:
        last = recently_sent.get(alert.key)
        if last is None or now - (last if last.tzinfo else last.replace(tzinfo=timezone.utc)) >= cooldown:
            if all(a.key != alert.key for a in out):
                out.append(alert)

    for i in created:
        if _wants(i.get("priority", "medium"), rules["new_issue_min_priority"]):
            label = issues.issue_number_label(int(i.get("number", 0)))
            add(Alert(f"issue:{i.get('number')}:new", "new_issue", i.get("priority", "medium"), f"{label}: {issues.title_for(i)}",
                      f"A new {i.get('priority')} priority problem was found.\n{issues.summary_for(i)}\n{base_url}/ops/llm/issues/{i.get('number')}"))
    if rules["reopened"]:
        for i in reopened:
            label = issues.issue_number_label(int(i.get("number", 0)))
            word = "came back soon after it was fixed" if i.get("regressed") else "came back"
            add(Alert(f"issue:{i.get('number')}:reopened", "reopened", i.get("priority", "medium"), f"{label} {word}: {issues.title_for(i)}",
                      f"{issues.summary_for(i)}\n{base_url}/ops/llm/issues/{i.get('number')}"))
    for q in quota:
        if q["level"] == "critical" and rules["quota_critical"]:
            add(Alert(f"quota:{q['provider']}:{q['model']}:{q['window']}:{q['window_start']}:critical", "quota_critical", "high",
                      f"{q['provider'].capitalize()} is almost out of {q['label'].lower()}",
                      f"{q['used']} of {q['limit']} used ({q['percent']}%). It resets at {q['reset_at']}."))
        elif q["level"] == "warning" and rules["quota_warning"]:
            add(Alert(f"quota:{q['provider']}:{q['model']}:{q['window']}:{q['window_start']}:warning", "quota_warning", "medium",
                      f"{q['provider'].capitalize()} is using a lot of {q['label'].lower()}",
                      f"{q['used']} of {q['limit']} used ({q['percent']}%). It resets at {q['reset_at']}."))
    if rules["provider_down"]:
        for name, state in providers.items():
            if state == "down":
                add(Alert(f"provider:{name}:down", "provider_down", "high", f"{name.capitalize()} is not working",
                          f"Most recent calls to {name.capitalize()} failed.\n{base_url}/ops/llm"))
    return out


async def _send_email(to: list[str], subject: str, body: str) -> bool:
    from app.core.config import settings
    from app.core.notifications import OPS_FROM

    if not to or not settings.RESEND_API_KEY:
        return False
    try:
        import resend

        resend.api_key = settings.RESEND_API_KEY
        html = "<p>" + body.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>") + "</p>"
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: resend.Emails.send({
            "from": OPS_FROM, "to": to, "subject": f"[Recast] {subject}", "html": html,
        }))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("LLM alert email failed: %s", exc)
        return False


async def _send_slack(text: str) -> bool:
    from app.core.config import settings

    if not settings.SLACK_WEBHOOK_URL:
        return False
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            res = await client.post(settings.SLACK_WEBHOOK_URL, json={"text": text}, timeout=5.0)
            return res.status_code < 300
    except Exception as exc:  # noqa: BLE001
        logger.error("LLM alert Slack post failed: %s", exc)
        return False


async def deliver(alert: Alert, rules: dict[str, Any]) -> dict[str, bool]:
    """Sends one alert through every channel that is set up, and logs it. Never raises."""
    from app.db.mongo import llm_alert_log

    rules = {**DEFAULT_RULES, **(rules or {})}
    result = {"email": await _send_email(list(rules["email_to"]), alert.subject, alert.body)}
    result["slack"] = await _send_slack(f"*{alert.subject}*\n{alert.body}") if rules["slack"] else False
    try:
        await llm_alert_log.insert_one({"at": datetime.now(timezone.utc), "key": alert.key, "kind": alert.kind, "subject": alert.subject, "result": result})
    except Exception:  # noqa: BLE001
        logger.debug("Could not log an LLM alert", exc_info=True)
    return result


async def recently_sent(keys: list[str], within: timedelta) -> dict[str, datetime]:
    from app.db.mongo import llm_alert_log

    if not keys:
        return {}
    since = datetime.now(timezone.utc) - within
    found: dict[str, datetime] = {}
    async for row in llm_alert_log.find({"key": {"$in": keys}, "at": {"$gte": since}}):
        found[row["key"]] = max(found.get(row["key"], row["at"]), row["at"])
    return found
