"""The creative prompts as functions: each builds its prompt, calls the model slot for that prompt, checks the answer, asks once for
a repair with the exact reasons when it fails, and says plainly what it could not fix. Nothing is guessed to fill a gap.

Every function returns `(result, meta)`. `meta` carries the prompt version, the model slot, how many tries it took, the language
problem if any, and the problems left, so the caller can store a record and a member is told when a result is only partly usable.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Optional

from app.core.config import settings
from app.prompts.registry import load_prompt, template_version
from app.video.create import validate as V

logger = logging.getLogger(__name__)

Caller = Callable[[str], Awaitable[dict[str, Any]]]

PROMPTS = (
    "hook_writer", "script_from_text", "script_from_images", "broll_queries", "caption_polish", "title_description_tags",
    "edit_plan", "qa_critic", "remy_voice_check",
)


def model_slot(name: str) -> str:
    """The model tier a prompt uses. Any prompt can move to a stronger one by setting VIDEO_PROMPT_MODEL_SLOTS, a JSON object such
    as {"edit_plan": "POWERFUL"}; nothing in code changes."""
    try:
        slots = json.loads(getattr(settings, "VIDEO_PROMPT_MODEL_SLOTS", "") or "{}")
    except ValueError:
        slots = {}
    return str(slots.get(name) or "BALANCED").upper()


def slot_caller(name: str) -> Caller:
    from app.shared.llm import GroqModel, call_llm_structured

    tier = model_slot(name)
    model = GroqModel[tier] if tier in GroqModel.__members__ else GroqModel.BALANCED

    async def call(prompt: str) -> dict[str, Any]:
        return await call_llm_structured(prompt, model=model, max_tokens=2200)

    return call


def language_rule(language: str) -> str:
    from app.pipelines.text.generator import build_language_instruction

    try:
        return build_language_instruction(language)
    except Exception:  # noqa: BLE001 - a missing instruction never blocks a prompt
        return ""


async def run(
    name: str, variables: dict[str, Any], validate: Callable[[Any], tuple[Any, list[str]]], *, language: str = "en",
    text_of: Optional[Callable[[Any], str]] = None, caller: Optional[Caller] = None, repairs: int = 1,
) -> tuple[Any, dict[str, Any]]:
    """Prompt, validate, repair once, report. `text_of(clean)` gives the text the language check reads."""
    from app.pipelines.text.language_check import language_problem

    caller = caller or slot_caller(name)
    prompt = load_prompt(f"video/{name}", language=language, language_rule=language_rule(language), **variables)

    def check(raw: Any) -> tuple[Any, list[str], Optional[str]]:
        clean, problems = validate(raw)
        lang = language_problem(text_of(clean), language) if text_of else None
        return clean, list(problems) + ([lang] if lang else []), lang

    tries = 1
    try:
        raw = await caller(prompt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s failed: %s", name, exc)
        raw = {}
    clean, problems, lang = check(raw)
    while problems and tries <= repairs:
        tries += 1
        repair = load_prompt("video/repair", problems=problems[:8], original=prompt)
        try:
            retry = await caller(repair)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s repair failed: %s", name, exc)
            break
        c2, p2, l2 = check(retry)
        if len(p2) <= len(problems):
            clean, problems, lang = c2, p2, l2
        if not p2:
            break
    meta = {"prompt": name, "prompt_version": template_version(f"video/{name}"), "model_slot": model_slot(name), "tries": tries,
            "language": language, "language_problem": lang, "problems": problems[:8]}
    return clean, meta


# ── the prompts ──────────────────────────────────────────────────────────────────────────────────────────────────────────

async def hooks(clip_text: str, *, language: str, voice: dict, caller: Optional[Caller] = None) -> tuple[list[dict], dict]:
    banned = voice.get("banned") or []
    return await run(
        "hook_writer", {"clip": clip_text[:4000], "brand_context": voice.get("context", ""), "banned": banned, "max_words": 14},
        lambda raw: V.validate_hooks(raw, clip_text, banned=banned), language=language,
        text_of=lambda c: " ".join(h["text"] for h in c), caller=caller,
    )


async def script_from_text(
    source: str, *, target_seconds: Optional[float], language: str, voice: dict, caller: Optional[Caller] = None,
) -> tuple[dict, dict]:
    hint = int((target_seconds or 0) / 60 * V.WPM.get(language.split("+")[0].split("-")[0], V.WPM["default"])) if target_seconds else 0
    return await run(
        "script_from_text", {"source": source[:6000], "brand_context": voice.get("context", ""), "target_seconds": target_seconds,
                             "words_hint": hint},
        lambda raw: V.validate_script(raw, source, target_seconds=target_seconds, language=language), language=language,
        text_of=lambda c: " ".join(b["narration"] for b in c["beats"]), caller=caller,
    )


async def picture_script(
    pictures: list[dict[str, str]], goal: str, *, language: str, voice: dict, caller: Optional[Caller] = None,
) -> tuple[list[dict], dict]:
    ids = [p["id"] for p in pictures]
    text = {p["id"]: f"{p.get('caption', '')} {p.get('alt', '')}" for p in pictures}
    listing = "\n".join(f"{p['id']}: {p.get('caption', '')} {p.get('alt', '')}".strip() for p in pictures)
    return await run(
        "script_from_images", {"pictures": listing, "goal": goal[:300] or "tell the story of these pictures", "brand_context": voice.get("context", "")},
        lambda raw: V.validate_picture_script(raw, ids, text), language=language,
        text_of=lambda c: " ".join(s["narration"] for s in c), caller=caller,
    )


async def broll(beat: str, mood: str, *, voice: dict, language: str = "en", caller: Optional[Caller] = None) -> tuple[dict, dict]:
    names = [n for n in [voice.get("name")] if n]
    return await run(
        "broll_queries", {"beat": beat[:600], "mood": mood[:60] or "neutral", "brand_note": (voice.get("context", "") or "")[:300]},
        lambda raw: V.validate_broll(raw, brand_names=names), language="en", caller=caller,
    )


async def polish_captions(
    words: list[dict[str, Any]], *, language: str, glossary: list[str], caller: Optional[Caller] = None,
) -> tuple[dict, dict]:
    listing = "\n".join(f"{i} {w['w']}" for i, w in enumerate(words))
    return await run(
        "caption_polish", {"words": listing, "count": len(words), "glossary": glossary[:30], "max_line_words": 7},
        lambda raw: V.validate_caption_polish(raw, words), language=language,
        text_of=lambda c: " ".join(w["w"] for w in c["words"]), caller=caller,
    )


PLATFORM_LABELS = {"youtube": "YouTube", "youtube_shorts": "YouTube Shorts", "instagram": "Instagram", "tiktok": "TikTok",
                   "linkedin": "LinkedIn", "facebook": "Facebook", "x": "X"}


async def publish_text(
    transcript: str, platform: str, *, chapters: list[dict[str, Any]], language: str, voice: dict, caller: Optional[Caller] = None,
) -> tuple[dict, dict]:
    lim = V.LIMITS.get(platform, V.LIMITS["youtube"])
    stamps = [_stamp(c["start_ms"]) for c in chapters]
    chapter_block = "\n".join(f"{_stamp(c['start_ms'])} {c['title']}" for c in chapters) if platform in ("youtube",) else ""
    return await run(
        "title_description_tags",
        {"transcript": transcript[:6000], "platform_label": PLATFORM_LABELS.get(platform, platform), "brand_context": voice.get("context", ""),
         "banned": voice.get("banned") or [], "title_limit": lim["title"], "description_limit": lim["description"], "tags_max": lim["tags_max"],
         "tags_total": lim["tags_total"], "chapters": chapter_block},
        lambda raw: V.validate_publish_text(raw, platform, stamps if chapter_block else [], banned=voice.get("banned") or []),
        language=language, text_of=lambda c: f"{c['title']} {c['description']}", caller=caller,
    )


def _stamp(ms: int) -> str:
    s = max(0, int(ms)) // 1000
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


STYLE_NOTES = {
    "tight": "Tight: remove every long pause and repeated take; keep the pace fast.",
    "natural": "Natural: only clear dead air and repeats; keep the speaker's rhythm.",
    "punchy": "Punchy: tight cuts, a few zooms on key lines and emphasised key phrases.",
}


async def edit_plan(
    transcript_lines: str, silences: list[tuple[int, int]], duration_ms: int, style: str, *, caller: Optional[Caller] = None,
) -> tuple[list[dict], dict]:
    listing = "\n".join(f"{s} {e}" for s, e in silences[:200])
    return await run(
        "edit_plan", {"transcript": transcript_lines[:7000], "silences": listing or "none", "duration_s": round(duration_ms / 1000, 1),
                      "style_note": STYLE_NOTES.get(style, STYLE_NOTES["natural"])},
        lambda raw: V.validate_edit_plan(raw, duration_ms, pauses=silences), language="en", caller=caller,
    )


async def critique(material: str, platform: str, *, language: str, voice: dict, caller: Optional[Caller] = None) -> tuple[list[dict], dict]:
    return await run(
        "qa_critic", {"material": material[:6000], "platform_label": PLATFORM_LABELS.get(platform, platform), "brand_context": voice.get("context", "")},
        lambda raw: V.validate_critique(raw), language=language, caller=caller,
    )


async def voice_check(
    lines: list[str], voice_summary: str, *, language: str, caller: Optional[Caller] = None,
) -> tuple[dict, dict]:
    listing = "\n".join(f"{i + 1} {t}" for i, t in enumerate(lines))
    return await run(
        "remy_voice_check", {"lines": listing, "voice_summary": voice_summary[:1500] or "No voice has been learned yet."},
        lambda raw: V.validate_voice_check(raw, lines), language=language, caller=caller,
    )
