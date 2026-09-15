"""AI-drafted voice-pattern suggestions for the brand-voice wizard's Manual
data step (openers, closers, signature phrases).

Replaces the retired "Extract" onboarding path as the actual fast option:
instead of typing 1-3 examples of each from a blank field, a user can
generate a starting set from the identity/audience/voice_tone they already
provided earlier in the wizard, then keep, edit, or discard each one
individually. Nothing here is ever persisted directly — the caller (the
suggest-voice-patterns route) only returns suggestions; PUT /brand/{id}/step
is still the only thing that writes to the brand profile.

Field lookups below intentionally mirror the verified snake_case-first,
camelCase-fallback reads in app/prompts/fragments/brand_context.jinja
(fixed for the same mismatch in an earlier pass) rather than re-deriving
them — see that file's history if the two ever need to be reconciled.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.prompts.registry import load_prompt
from app.shared.llm import GroqModel, call_llm_structured

logger = logging.getLogger(__name__)

_VALID_PLACEMENTS = {"hook", "transition", "close", "any"}
_MAX_OPENERS = 5
_MAX_CLOSERS = 5
_MAX_PHRASES = 6


# ─────────────────────────────────────────────────────────────────────────────
# Context summarisation — pure functions, no I/O, easy to unit test directly.
# ─────────────────────────────────────────────────────────────────────────────

def _identity_summary(brand_type: str, identity: dict) -> str:
    """One-line description of who/what the brand is, per brand_type."""
    identity = identity or {}
    parts: list[str] = []

    if brand_type == "Person":
        if identity.get("name"):
            parts.append(f"Name: {identity['name']}")
        if identity.get("profession"):
            parts.append(f"Profession: {identity['profession']}")
        if identity.get("bio"):
            parts.append(f"Background: {identity['bio']}")
    elif brand_type == "Business":
        name = identity.get("company_name") or identity.get("companyName")
        if name:
            parts.append(f"Company: {name}")
        if identity.get("description"):
            parts.append(f"What they do: {identity['description']}")
        if identity.get("industry"):
            parts.append(f"Industry: {identity['industry']}")
    elif brand_type == "Personal Brand":
        if identity.get("name"):
            parts.append(f"Brand: {identity['name']}")
        if identity.get("tagline"):
            parts.append(f"Tagline: {identity['tagline']}")
        if identity.get("mission"):
            parts.append(f"Mission: {identity['mission']}")
    elif brand_type == "Product":
        name = identity.get("product_name") or identity.get("productName")
        if name:
            parts.append(f"Product: {name}")
        if identity.get("description"):
            parts.append(f"What it does: {identity['description']}")

    return " | ".join(parts)


def _audience_summary(audience: dict) -> str:
    audience = audience or {}
    parts: list[str] = []
    if audience.get("primary_pain_point"):
        parts.append(audience["primary_pain_point"])
    if audience.get("reading_level"):
        parts.append(f"reads at a {str(audience['reading_level']).lower()} level")
    return "; ".join(parts)


def _tone_summary(voice_tone: dict) -> str:
    voice_tone = voice_tone or {}
    parts: list[str] = []
    tones = voice_tone.get("tones") or []
    if isinstance(tones, list) and tones:
        parts.append(", ".join(str(t) for t in tones))
    if voice_tone.get("humor"):
        parts.append(f"humor level: {voice_tone['humor']}")
    if voice_tone.get("emoji"):
        parts.append(f"emoji use: {voice_tone['emoji']}")
    return "; ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Response cleaning — never trust a raw LLM dict; wrong types, missing keys,
# and invalid enum values are all stripped rather than propagated.
# ─────────────────────────────────────────────────────────────────────────────

def _clean_string_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for v in value:
        if not isinstance(v, str):
            continue
        v = v.strip()
        # Case-insensitive de-dup — a model repeating near-identical
        # suggestions is more useful shown once than twice.
        if not v or v.lower() in seen:
            continue
        seen.add(v.lower())
        cleaned.append(v)
    return cleaned[:limit]


def _clean_phrases(value: Any, limit: int) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text or text.lower() in seen:
            continue
        placement = str(item.get("placement", "any")).strip().lower()
        if placement not in _VALID_PLACEMENTS:
            placement = "any"
        seen.add(text.lower())
        cleaned.append({"text": text, "placement": placement})
    return cleaned[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

async def generate_voice_pattern_suggestions(
    brand_profile: dict,
) -> Optional[dict[str, Any]]:
    """Draft openers/closers/phrases from an existing brand profile's
    identity, audience, and voice_tone.

    Returns None (never raises) if the LLM call fails outright or the
    cleaned result is entirely empty — the caller is expected to turn that
    into a friendly "couldn't generate, write your own" response rather
    than a raw 500.
    """
    brand_type = brand_profile.get("brand_type", "Person")
    identity_line = _identity_summary(brand_type, brand_profile.get("identity") or {})
    audience_line = _audience_summary(brand_profile.get("audience") or {})
    tone_line = _tone_summary(brand_profile.get("voice_tone") or {})
    style = (brand_profile.get("voice_tone") or {}).get("style", "")

    prompt = load_prompt(
        "brand/suggest_voice_patterns",
        brand_type=brand_type,
        identity_line=identity_line,
        audience_line=audience_line,
        tone_line=tone_line,
        style=style,
    )

    try:
        result = await call_llm_structured(
            prompt=prompt,
            system=(
                "You are a ghostwriter drafting starter voice-pattern examples "
                "for a new client's content brand voice. Return JSON only."
            ),
            model=GroqModel.BALANCED,
            max_tokens=1200,
        )
    except Exception:  # noqa: BLE001 - never let a suggestion feature break onboarding
        logger.exception("voice pattern suggestion generation raised unexpectedly")
        return None

    if not result:
        return None

    openers = _clean_string_list(result.get("openers"), _MAX_OPENERS)
    closers = _clean_string_list(result.get("closers"), _MAX_CLOSERS)
    phrases = _clean_phrases(result.get("phrases"), _MAX_PHRASES)

    if not openers and not closers and not phrases:
        return None

    return {"openers": openers, "closers": closers, "phrases": phrases}
