"""The few social links worth putting in a YouTube description.

Only channels that matter for a viewer who wants to follow the brand
(LinkedIn, Instagram, Facebook, Threads), and only ones that actually exist:
a link the member typed for the brand wins, otherwise the profile URL of an
account they've connected. Nothing is guessed or invented — an empty result
just means the description gets no "Follow" block.
"""

from typing import Optional

from app.pipelines.publish.token_store import get_all_tokens

# (key, label) in the order they appear in the description.
SOCIAL_LINKS: list[tuple[str, str]] = [
    ("linkedin", "LinkedIn"),
    ("instagram", "Instagram"),
    ("facebook", "Facebook"),
    ("threads", "Threads"),
]


def clean_url(value: object) -> Optional[str]:
    """A usable https URL, or None. Accepts "instagram.com/x" typed without a
    scheme; rejects anything that doesn't look like a web address."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v or " " in v:
        return None
    if v.lower().startswith(("http://", "https://")):
        return v
    if "." in v.split("/")[0]:
        return f"https://{v}"
    return None


async def collect_social_links(workspace_id: str, brand_profile: dict) -> list[tuple[str, str]]:
    manual = (brand_profile.get("visual_identity") or {}).get("social_links") or {}
    connected: dict[str, Optional[str]] = {}
    try:
        for account in await get_all_tokens(workspace_id):
            connected[account["platform"]] = account.get("profile_url")
    except Exception:  # noqa: BLE001 — a lookup failure must never block preparing an upload
        connected = {}

    links: list[tuple[str, str]] = []
    for key, label in SOCIAL_LINKS:
        url = clean_url(manual.get(key)) or clean_url(connected.get(key))
        if url:
            links.append((label, url))
    return links
