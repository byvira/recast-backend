"""Rewrite a narration script so it runs about as long as the member wants ("Expand to fit" and
"Trim to fit"). One AI call and at most one retry. Every answer is checked before it is used:
not empty, no instruction text leaked in, and close enough to the target length. When a too-long
script cannot be rewritten well, it is cut at a sentence end instead; when a too-short one
cannot be expanded well, it is returned unchanged with the reason, never a bad rewrite."""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from app.pipelines.media import duration
from app.prompts.registry import load_prompt
from app.shared.localized_strings import clean_translation, looks_leaked

logger = logging.getLogger(__name__)

MAX_SCRIPT_CHARS = 20000
# A rewrite is accepted when it lands within these bounds of the target length.
LOWEST_FIT = 0.85
HIGHEST_FIT = 1.15

LLM = Callable[[str], Awaitable[str]]


def acceptable(text: str, source: str, target_seconds: float, wpm: Optional[float]) -> bool:
    out = (text or "").strip()
    if not out or looks_leaked(out, source):
        return False
    ratio = duration.estimate_seconds(out, wpm) / float(target_seconds)
    return LOWEST_FIT <= ratio <= HIGHEST_FIT


async def fit_script(
    script: str, target_seconds: float, wpm: Optional[float], language_name: str, llm: LLM,
) -> dict:
    """Returns {"script", "status", "estimated_seconds", "reason"}.
    status: "already_fits", "fitted" (script is the new version) or "unchanged" (could not do it well)."""
    script = (script or "").strip()[:MAX_SCRIPT_CHARS]
    target = float(duration.clamp_target_seconds(target_seconds))
    assessment = duration.fit_assessment(script, target, wpm)
    if not script or assessment["status"] == "ok":
        return _result(script, "already_fits", wpm, "")

    direction = "shorter" if assessment["status"] == "long" else "longer"
    prompt = load_prompt(
        "audio/fit_script",
        script=script,
        direction=direction,
        target_seconds=int(target),
        target_words=assessment["words_needed"],
        current_words=assessment["words"],
        language_name=language_name,
    )
    for _ in range(2):
        try:
            out = clean_translation(await llm(prompt))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Script fit rewrite failed: %s", exc)
            break
        if acceptable(out, script, target, wpm):
            return _result(out, "fitted", wpm, "")

    if direction == "shorter":
        return _result(duration.trim_to_seconds(script, target, wpm), "fitted", wpm, "")
    return _result(script, "unchanged", wpm, "Couldn't lengthen this script well. Add a little yourself, or try again.")


def _result(script: str, status: str, wpm: Optional[float], reason: str) -> dict:
    return {
        "script": script,
        "status": status,
        "estimated_seconds": duration.estimate_seconds(script, wpm),
        "reason": reason,
    }
