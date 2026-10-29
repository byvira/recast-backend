"""The credit costs table: what each action costs, as data.

One credit is the unit everywhere. An action costs a fixed number of credits, either once (`per` is None) or for each unit of work
(`per` names the unit: a minute of narration, 1,000 words translated, 10 minutes of video analysed). Ops can change a cost without
a code change; a saved copy in `billing_credit_costs` wins when present, read through `costs()`.

`kind` says who starts the work: "active" actions are things a member (or an automation a member set up) asked for, and are charged;
"passive" actions are system work nobody pressed a button for (safety checks, Odette, Remy's background check) and are always free.
They are listed so the costs table, the runbook and Ops show every place Ekavo does AI work, not only the paid ones.
"""
from __future__ import annotations

import math
import time
from typing import Any, Optional

from app.db.mongo import get_db

COSTS_CACHE_SECONDS = 60

# module: which area of the product the action belongs to, in the order screens list them.
MODULES: dict[str, str] = {
    "text": "Posts and writing",
    "picture": "Pictures",
    "audio": "Voice and audio",
    "video": "Video",
    "campaign": "Campaigns",
    "remy": "Remy",
    "ask": "Ask Ekavo",
    "system": "Always free",
}

# per: None charges `credits` once; otherwise `credits` for each `per_size` units of `per`, rounded up to a whole credit.
DEFAULT_COSTS: dict[str, dict[str, Any]] = {
    # Text
    "text_post": {"module": "text", "label": "Post for one platform (write, regenerate or rewrite)", "credits": 1, "kind": "active"},
    "long_form": {"module": "text", "label": "Long form (blog, newsletter, script, YouTube description)", "credits": 3, "kind": "active"},
    "text_refine": {"module": "text", "label": "Refine or quick action on a post", "credits": 1, "kind": "active"},
    "reader_review": {"module": "text", "label": "Reader review of a draft", "credits": 1, "kind": "active"},
    "translate": {"module": "text", "label": "Translate or localise", "credits": 1, "per": "words", "per_size": 1000, "kind": "active"},
    "hooks_angles": {"module": "text", "label": "Hooks, angles, scoring, hashtags, first comment", "credits": 0, "kind": "active"},
    "preset_suggest": {"module": "text", "label": "Preset structure suggestions", "credits": 0, "kind": "active"},
    # Pictures
    "picture": {"module": "picture", "label": "AI picture or regenerate", "credits": 2, "kind": "active"},
    "default_picture": {"module": "picture", "label": "Picture made with a post (can be switched off)", "credits": 2, "kind": "active"},
    "picture_hd": {"module": "picture", "label": "High-detail picture", "credits": 8, "kind": "active"},
    "remove_background": {"module": "picture", "label": "Remove background", "credits": 1, "kind": "active"},
    "mascot_first": {"module": "picture", "label": "Brand mascot, first one", "credits": 0, "kind": "active"},
    "mascot": {"module": "picture", "label": "Brand mascot, made again", "credits": 2, "kind": "active"},
    # Audio
    "narration": {"module": "audio", "label": "Narration", "credits": 3, "per": "minutes", "per_size": 1, "kind": "active"},
    "dialogue": {"module": "audio", "label": "Two-voice dialogue", "credits": 3, "per": "minutes", "per_size": 1, "kind": "active"},
    "transcription": {"module": "audio", "label": "Transcription", "credits": 1, "per": "minutes", "per_size": 1, "kind": "active"},
    "cleanup": {"module": "audio", "label": "Cleanup", "credits": 1, "per": "minutes", "per_size": 1, "kind": "active"},
    "audio_mix": {"module": "audio", "label": "Mix an episode", "credits": 1, "per": "minutes", "per_size": 1, "kind": "active"},
    "audio_script": {"module": "audio", "label": "Audio script, chapters, clip or soundbite picks", "credits": 1, "kind": "active"},
    # Video
    "render": {"module": "video", "label": "Video render", "credits": 5, "per": "minutes", "per_size": 1, "kind": "active"},
    "captions": {"module": "video", "label": "Captions from speech", "credits": 1, "per": "minutes", "per_size": 1, "kind": "active"},
    "video_plan": {"module": "video", "label": "Prompt-to-video or long-form plan", "credits": 3, "kind": "active"},
    "video_script": {"module": "video", "label": "Video script", "credits": 1, "kind": "active"},
    "video_analysis": {"module": "video", "label": "Video analysis", "credits": 1, "per": "minutes", "per_size": 10, "kind": "active"},
    "video_extras": {"module": "video", "label": "Hooks, covers text, stock and sound picks", "credits": 0, "kind": "active"},
    # Campaigns
    "campaign_plan": {"module": "campaign", "label": "Planning a campaign", "credits": 5, "kind": "active"},
    "campaign_topics": {"module": "campaign", "label": "Campaign topic ideas", "credits": 0, "kind": "active"},
    # Remy
    "voice_analysis": {"module": "remy", "label": "Voice analysis or calibration", "credits": 3, "kind": "active"},
    "draft_feedback": {"module": "remy", "label": "Feedback on a draft", "credits": 1, "kind": "active"},
    # Ask Ekavo
    "ask_step": {"module": "ask", "label": "Ask Ekavo, each planning step", "credits": 1, "kind": "active"},
    # System work nobody pressed a button for: always free.
    "content_guard": {"module": "system", "label": "Safety checks (Content Guard), including at publish", "credits": 0, "kind": "passive"},
    "odette": {"module": "system", "label": "Odette briefings, alerts and background checks", "credits": 0, "kind": "passive"},
    "remy_drift": {"module": "system", "label": "Remy's background voice check", "credits": 0, "kind": "passive"},
    "describe_upload": {"module": "system", "label": "Describing an upload", "credits": 0, "kind": "passive"},
    "analytics_answer": {"module": "system", "label": "The analytics answer", "credits": 0, "kind": "passive"},
    "support_assistant": {"module": "system", "label": "Support assistant and ticket sorting", "credits": 0, "kind": "passive"},
    "typing_help": {"module": "system", "label": "Typing suggestions", "credits": 0, "kind": "passive"},
    "publish_details": {"module": "system", "label": "YouTube details and translated labels at publish", "credits": 0, "kind": "passive"},
}

UNIT_WORDS: dict[str, tuple[str, str]] = {"minutes": ("minute", "minutes"), "words": ("word", "words")}


def unit_label(cost: dict[str, Any]) -> Optional[str]:
    """"per minute", "per 1,000 words", "per 10 minutes", or None for a cost charged once."""
    per = cost.get("per")
    if not per:
        return None
    size = int(cost.get("per_size") or 1)
    one, many = UNIT_WORDS.get(per, (per, per))
    return f"per {one}" if size == 1 else f"per {size:,} {many}"


# ── the table, with Ops changes on top ──────────────────────────────────

_cache: tuple[float, dict[str, dict[str, Any]]] | None = None


def _collection():
    return get_db()["billing_credit_costs"]


async def costs() -> dict[str, dict[str, Any]]:
    """Every action's cost: the defaults with any value saved from Ops on top. Cached for a minute; `forget` clears it."""
    global _cache
    now = time.monotonic()
    if _cache and _cache[0] > now:
        return _cache[1]
    table = {key: dict(value) for key, value in DEFAULT_COSTS.items()}
    try:
        async for saved in _collection().find({"id": {"$in": list(DEFAULT_COSTS)}}):
            if isinstance(saved.get("credits"), int) and saved["credits"] >= 0:
                table[saved["id"]]["credits"] = saved["credits"]
    except Exception:  # noqa: BLE001 - the defaults are always a safe answer
        pass
    _cache = (now + COSTS_CACHE_SECONDS, table)
    return table


def forget() -> None:
    global _cache
    _cache = None


def price_with(table: dict[str, dict[str, Any]], action: str, units: float = 1) -> int:
    """Credits for `units` of an action, from a given table. A cost per unit is rounded up to a whole credit, so 1.2 minutes of
    narration at 3 a minute is 4. Any real amount of work on a paid action costs at least 1."""
    cost = table[action]
    credits = int(cost.get("credits") or 0)
    if credits <= 0:
        return 0
    if not cost.get("per"):
        return credits
    size = float(cost.get("per_size") or 1)
    amount = max(0.0, float(units or 0)) / size
    if amount <= 0:
        return 0
    return max(1, math.ceil(round(credits * amount, 6)))


async def price(action: str, units: float = 1) -> int:
    return price_with(await costs(), action, units)


def public_row(key: str, cost: dict[str, Any]) -> dict[str, Any]:
    default = DEFAULT_COSTS[key]
    return {
        "id": key, "module": cost["module"], "module_label": MODULES[cost["module"]], "label": cost["label"],
        "credits": int(cost.get("credits") or 0), "default_credits": int(default.get("credits") or 0),
        "per": cost.get("per"), "per_size": cost.get("per_size"), "unit_label": unit_label(cost), "kind": cost["kind"],
    }
