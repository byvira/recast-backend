"""The brand's voice as the creative prompts use it, with a version that changes whenever the voice does.

Every piece of text made for a video records which voice version wrote it, so a later change to the brand can be told apart from a
change in the model, and a member can see which pieces were written in an older voice.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from app.db.mongo import brand_profiles


def banned_words(profile: Optional[dict]) -> list[str]:
    manual = (profile or {}).get("manual_data") or {}
    raw = manual.get("banned_words") or manual.get("bannedWords") or []
    return [str(w).strip() for w in raw if str(w).strip()][:60]


def voice_version(profile: Optional[dict], context: str, banned: list[str]) -> str:
    """A short fingerprint of everything that shapes the voice: the context text, the banned words and the profile's own
    last-changed time. Same voice, same version."""
    stamp = str((profile or {}).get("updated_at") or "")
    return hashlib.sha1(json.dumps([context, banned, stamp], ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


async def load(workspace_id: str, brand_id: Optional[str]) -> dict[str, Any]:
    """{"brand_id", "name", "context", "banned", "version", "known"}. With no brand the context is empty and the version says
    so: text is then written plainly and the record says no brand voice was used."""
    profile = None
    if brand_id:
        profile = await brand_profiles.find_one({"id": brand_id, "workspace_id": workspace_id})
    else:
        profile = await brand_profiles.find_one({"workspace_id": workspace_id}, sort=[("created_at", 1)])
    if not profile:
        return {"brand_id": None, "name": None, "context": "", "banned": [], "version": "none", "known": False}
    from app.pipelines.text.brand_context import build_brand_context

    context = build_brand_context(profile)[:2500]
    banned = banned_words(profile)
    name = ((profile.get("identity") or {}).get("brand_name") or (profile.get("identity") or {}).get("name") or profile.get("brand_type"))
    return {"brand_id": profile.get("id"), "name": name, "context": context, "banned": banned,
            "version": voice_version(profile, context, banned), "known": True}
