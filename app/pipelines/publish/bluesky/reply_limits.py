"""Bluesky reply limits. Who can reply to a post is a "threadgate" record in the member's own repository, saved under the same key as the
post. An empty list of rules means nobody can reply; each rule lets one group in. Written once the post exists; it is not confirmed
against a live account yet.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

THREADGATE = "app.bsky.feed.threadgate"

#: Our choice to the rules the platform names. Anyone is the default and needs no record.
RULES: dict[str, list[dict]] = {
    "nobody": [],
    "mentioned": [{"$type": "app.bsky.feed.threadgate#mentionRule"}],
    "followers": [{"$type": "app.bsky.feed.threadgate#followerRule"}],
    "following": [{"$type": "app.bsky.feed.threadgate#followingRule"}],
}


def gate_record(post_uri: str, choice: Optional[str], now: Optional[datetime] = None) -> Optional[dict]:
    """The record that applies a choice to a post, or None when anyone can reply."""
    if choice not in RULES:
        return None
    return {
        "$type": THREADGATE,
        "post": post_uri,
        "allow": RULES[choice],
        "createdAt": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }


async def limit_replies(
    client: httpx.AsyncClient, *, pds_base: str, access_token: str, did: str, post_uri: str, choice: Optional[str],
) -> Optional[str]:
    """Saves the reply limit for a post that is out. Returns a plain note when it could not be saved, otherwise None."""
    record = gate_record(post_uri, choice)
    if record is None:
        return None
    try:
        response = await client.post(
            f"{pds_base}/com.atproto.repo.createRecord",
            json={"repo": did, "collection": THREADGATE, "rkey": post_uri.rsplit("/", 1)[-1], "record": record},
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        )
        if response.status_code == 200:
            return None
        logger.warning("Bluesky reply limit failed: %s %s", response.status_code, response.text[:200])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Bluesky reply limit failed: %s", exc)
    return "The post went out, but the reply limit could not be set. Anyone can reply."
