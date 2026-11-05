"""Text a member brings in that tries to give the writing assistant orders.

A pasted article, a page we fetch or a transcript is material to write from. Sometimes it contains sentences aimed at the assistant
itself ("ignore the above and..."). The prompts already fence such material and tell the model to treat it as data, so nothing is
blocked here; this notes it, so Ops can see where it comes from and the workspace's admins are told. It never stops a run, never
changes the text, and never raises.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.db.mongo import get_db

logger = logging.getLogger(__name__)

EXCERPT = 200
_FLAGS: list[tuple[str, re.Pattern[str]]] = [
    ("asks the writer to ignore its instructions", re.compile(r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all|any|your|the)\b[^.\n]{0,30}\b(?:instructions?|rules?|prompts?|guidelines?|directions?)\b", re.I)),
    ("tries to change who the writer is", re.compile(r"\b(?:(?:you are now|from now on,? you(?:'re| are)?)\s+(?:now\s+)?(?:an?\s+|the\s+)?(?:unrestricted|uncensored|unfiltered|jailbroken|evil|developer|system|admin|root)|"
                                                      r"pretend (?:to be|that you are|you are)\s+(?:an?\s+)?(?:unrestricted|uncensored|unfiltered|jailbroken|different ai|another ai|the system|developer)|"
                                                      r"act as (?:if you are |an? )?(?:unrestricted|uncensored|unfiltered|jailbroken|dan\b|developer))", re.I)),
    ("asks for the hidden instructions", re.compile(r"\b(?:reveal|show|print|repeat|output|leak)\b[^.\n]{0,40}\b(?:system prompt|your (?:instructions|prompt|rules)|hidden (?:instructions|prompt))\b", re.I)),
    ("uses model control markers", re.compile(r"(?:<\|?(?:system|assistant|im_start|im_end)\|?>|\[/?INST\]|<<SYS>>|###\s*(?:system|instruction))", re.I)),
    ("asks to switch off safety", re.compile(r"\b(?:jailbreak|developer mode|do anything now|without (?:any )?(?:restrictions|filters|safety)|bypass (?:the )?(?:safety|filters?|guardrails?))\b", re.I)),
]


def scan(text: str) -> list[dict[str, str]]:
    """What in the text reads as an order to the assistant: a short reason and the words around it. Empty for ordinary text."""
    found: list[dict[str, str]] = []
    for reason, pattern in _FLAGS:
        m = pattern.search(text or "")
        if m:
            start = max(0, m.start() - 40)
            found.append({"reason": reason, "excerpt": " ".join(text[start:m.end() + 60].split())[:EXCERPT]})
    return found


def col():
    return get_db()["safety_input_flags"]


async def check_source(text: str, *, workspace_id: Optional[str], user_id: Optional[str], where: str, source: str = "pasted text") -> list[dict[str, str]]:
    """Scan a source and keep a note of anything found. Returns what was found; the caller carries on whatever it is."""
    try:
        found = scan(text)
        if not found:
            return []
        from app.agents.content_guard.sensitive import mask_personal

        digest = hashlib.sha256(f"{workspace_id}|{found[0]['excerpt']}".encode("utf-8")).hexdigest()[:32]
        now = datetime.now(timezone.utc)
        res = await col().update_one(
            {"fingerprint": digest},
            {"$setOnInsert": {"fingerprint": digest, "workspace_id": workspace_id, "user_id": user_id, "where": where, "source": source,
                              "reasons": [f["reason"] for f in found], "excerpt": mask_personal(found[0]["excerpt"]), "created_at": now},
             "$set": {"last_seen_at": now}, "$inc": {"times": 1}},
            upsert=True,
        )
        if res.upserted_id is not None and workspace_id:
            await _tell_admins(workspace_id, user_id, found, source, digest)
        return found
    except Exception as exc:  # noqa: BLE001 - a note about a source must never stop the work
        logger.warning("Input check failed: %s", exc)
        return []


async def _tell_admins(workspace_id: str, user_id: Optional[str], found: list[dict[str, str]], source: str, digest: str) -> None:
    try:
        from app.shared.activity.i18n import phrase, words
        from app.shared.activity.projector import record_system

        await record_system(
            workspace_id=workspace_id, key=f"input-flag:{digest}", actor_name="Ekavo safety", actor_user_id=user_id, category="safety", status="info",
            title=phrase("A source contained instructions aimed at the writer"),
            description=phrase("The {source} had text that tries to give orders to the writing assistant ({reason}). It was used as material only.", source=source, reason=words(found[0]["reason"])),
            visibility="admins", metadata={"event": "safety.input_flag"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not tell the admins about a flagged source: %s", exc)


async def recent(limit: int = 100) -> list[dict[str, Any]]:
    rows = await col().find({}, {"_id": 0}).sort("last_seen_at", -1).limit(min(max(limit, 1), 200)).to_list(200)
    return rows
