"""Decide candidates built only on free tiers, to be judged against the live small model (see `decisions.py`).

* Other Groq models (free tier, the main provider, so private text is allowed): the model names come from `ASK_CANDIDATE_MODELS` (comma separated, empty by default because Groq's older models were removed), so a change in what
  Groq offers is a setting, not a code change. Each is a named candidate, for example `groq:<model>`. They are tried in shadow mode or on the harness only.
* A translator pivot (`pivot_m2m100`): the request is translated to English with Cloudflare's free M2M100 model, then the live small model decides on the English
  text. It helps a language the deciding model is weak at, costs nothing (it draws on the same free daily allowance as embeddings) and is OFF until the owner agrees to
  member text going to Cloudflare for translation (`ASK_PIVOT_ENABLED`). Latin-script Tamil or Hindi (Tanglish, Hinglish) is detected as English, so it is not
  translated: the harness will show whether the live model copes with those unaided.

Nothing here is acted on: a candidate only ever runs in the harness or in shadow mode."""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.ask import decisions, fence, pipeline, router
from app.core.config import settings
from app.shared.language import detect_language
from app.tools.contract import ToolSpec

logger = logging.getLogger(__name__)

TRANSLATE_MODEL = "@cf/meta/m2m100-1.2b"
TIMEOUT_S = 20.0
MAX_PIVOT_CHARS = 1000


def candidate_models() -> list[str]:
    return [m.strip() for m in (settings.ASK_CANDIDATE_MODELS or "").split(",") if m.strip()]


async def _groq_json(model: str, prompt: str, system: str) -> dict[str, Any]:
    """One structured answer from a named Groq model, as the main provider (private content). Raises on any failure so the harness counts it."""
    from app.shared.llm import get_groq_client, private_content

    async with private_content():
        reply = await get_groq_client().chat.completions.create(
            model=model, messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}], temperature=0, max_tokens=300,
            response_format={"type": "json_object"},
        )
    return json.loads(reply.choices[0].message.content or "{}")


def groq_candidate(model: str) -> decisions.Decider:
    async def decide(text: str, candidates: list[ToolSpec], language: str) -> decisions.Decision:
        prompt, system = pipeline.decide_prompt(text, candidates, language)
        raw = await _groq_json(model, prompt, system)
        action, tool, _question, _reason = pipeline.parse_decision(raw, {s.name for s in candidates})
        return decisions.Decision("tool", tool) if action == "tool" and tool else decisions.Decision("none")

    return decide


async def _translate(text: str, source: str) -> str:
    """The text in English by Cloudflare's free M2M100. Raises on failure."""
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        r = await client.post(f"https://api.cloudflare.com/client/v4/accounts/{settings.CLOUDFLARE_ACCOUNT_ID}/ai/run/{TRANSLATE_MODEL}",
                              headers={"Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}"}, json={"text": text[:MAX_PIVOT_CHARS], "source_lang": source, "target_lang": "en"})
        r.raise_for_status()
    out = ((r.json().get("result") or {}).get("translated_text") or "").strip()
    if not out:
        raise ValueError("empty translation")
    return out


async def pivot_m2m100(text: str, candidates: list[ToolSpec], language: str) -> decisions.Decision:
    """Translate to English (when the request is in another language), then let the live deciding model choose on the English words."""
    source = (language or detect_language(text) or "en")[:2].lower()
    english = text if source == "en" else await _translate(fence.clean(text, MAX_PIVOT_CHARS), source)
    return await decisions.live(english, candidates, "en")


def register() -> list[str]:
    """Add the candidates this deployment allows. Returns their names."""
    names: list[str] = []
    if settings.GROQ_API_KEY:
        for model in candidate_models():
            name = f"groq:{model}"
            decisions.CANDIDATES[name] = groq_candidate(model)
            names.append(name)
    if settings.ASK_PIVOT_ENABLED and settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ACCOUNT_ID:
        decisions.CANDIDATES["pivot_m2m100"] = pivot_m2m100
        names.append("pivot_m2m100")
    return names


register()
