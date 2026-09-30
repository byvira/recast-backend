"""AI suggestion for a template's sections. Read-only: nothing is saved; the member edits the
result in the step builder. A bad or empty model answer gives an empty suggestion, never an error,
so building a template by hand always still works."""

from __future__ import annotations

import logging
from typing import Any

from app.prompts.registry import load_prompt
from app.shared.llm import call_llm_structured

logger = logging.getLogger(__name__)

MIN_STEPS = 2
MAX_STEPS = 8
MIN_CHARS = 20
MAX_CHARS = 3000


def clean_sections(raw: Any) -> list[dict]:
    """Keep only well-formed sections, in order: a real name, a limit clamped to a sensible range,
    a one-line instruction. Repeated names are dropped. Returns [] when fewer than MIN_STEPS survive
    (one section is not a structure)."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("section_name") or "").strip()[:60]
        guide = " ".join(str(item.get("guidelines") or "").split())[:240]
        if not name or not guide or name.lower() in seen:
            continue
        try:
            limit = int(item.get("char_limit"))
        except (TypeError, ValueError):
            limit = 280
        seen.add(name.lower())
        out.append({"section_name": name, "char_limit": max(MIN_CHARS, min(MAX_CHARS, limit)), "guidelines": guide})
        if len(out) == MAX_STEPS:
            break
    return out if len(out) >= MIN_STEPS else []


async def suggest_structure(
    *, title: str, category_label: str, description: str, channels: list[str], existing_steps: list[dict],
) -> list[dict]:
    prompt = load_prompt(
        "presets/suggest_structure",
        title=title[:200],
        category_label=category_label[:80],
        description=description[:600],
        channels=channels[:12],
        existing_steps=existing_steps[:8],
    )
    result = await call_llm_structured(prompt, max_tokens=900)
    sections = clean_sections((result or {}).get("sections"))
    if not sections:
        logger.warning("Template structure suggestion returned nothing usable")
    return sections
