"""Next steps the support assistant can offer a member, as links to the screen where the fix is done.

They come from the same snapshot the assistant is told about (plain rules, no model), and only ever point at pages the member can
open themselves. Nothing is changed for the member: they press the link and do it there. The model never chooses a link, so a reply
can never send someone somewhere that was not worked out from their own workspace."""
from __future__ import annotations

from typing import Any

#: The most links shown under one reply.
MAX_ACTIONS = 3
CONNECTIONS = "/dashboard/settings?tab=nodes"


def suggest_actions(snapshot: dict[str, Any], *, out_of_credits: bool = False) -> list[dict[str, str]]:
    """Up to three links, the most likely fix first. Each is {"label", "href"}."""
    found: list[dict[str, str]] = []

    def add(label: str, href: str) -> None:
        if all(a["href"] != href or a["label"] != label for a in found):
            found.append({"label": label, "href": href})

    for p in snapshot.get("platforms") or []:
        if p.get("health") in ("expired", "expiring_soon", "disconnected"):
            add(f"Reconnect {str(p.get('platform') or 'the account').capitalize()}", CONNECTIONS)
    if out_of_credits:
        add("See your credits and plan", "/dashboard/settings?tab=billing")
    if (snapshot.get("workspace") or {}).get("generation_halted"):
        add("Open Odette to resume writing", "/dashboard/odette")
    schedule = snapshot.get("schedule") or {}
    if int(schedule.get("overdue") or 0):
        add("See your scheduled posts", "/dashboard/calendar")
    if any(c.get("status") == "paused" for c in snapshot.get("campaigns") or []):
        add("Open your campaigns", "/dashboard/campaigns")
    if len(snapshot.get("errors") or []) >= 3:
        add("See what failed in your activity", "/dashboard/activity")
    return found[:MAX_ACTIONS]
