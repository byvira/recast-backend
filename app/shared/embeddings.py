"""Text embeddings with backups, for callers that compare vectors made in the same call (Ask Ekavo's tool shortlist, for one).

Order: Gemini (`embed_text`), then Mistral `mistral-embed` (a separate vendor), then Cloudflare `bge-m3` (multilingual). Every answer says which
model made it (`space`), because vectors from different models cannot be compared with each other: a caller keeps and compares vectors only
within one space, and never against a vector that was stored earlier in another one. The member's stored voice baseline therefore keeps using
`embed_text` alone, whose vectors are all in Gemini's space.

A provider without a key is skipped. Every attempt goes through `track`, so it shows on the LLM health page like any other fallback."""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.core.config import settings
from app.shared.llm_health import probe
from app.shared.llm_health.track import fallback_scope, track

logger = logging.getLogger(__name__)

TIMEOUT_S = 30.0
MAX_CHARS = 8000


def _unit(values: list[float]) -> list[float]:
    norm = sum(v * v for v in values) ** 0.5
    return [v / norm for v in values] if norm else []


async def _mistral(texts: list[str]) -> list[list[float]]:
    async with track("mistral", settings.MISTRAL_EMBED_MODEL, feature="embeddings"):
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            r = await client.post("https://api.mistral.ai/v1/embeddings", headers={"Authorization": f"Bearer {settings.MISTRAL_API_KEY}"},
                                  json={"model": settings.MISTRAL_EMBED_MODEL, "input": texts})
            r.raise_for_status()
        return [row["embedding"] for row in sorted(r.json()["data"], key=lambda row: row["index"])]


async def _cloudflare(texts: list[str]) -> list[list[float]]:
    async with track("cloudflare", settings.CLOUDFLARE_EMBED_MODEL, feature="embeddings"):
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            r = await client.post(f"https://api.cloudflare.com/client/v4/accounts/{settings.CLOUDFLARE_ACCOUNT_ID}/ai/run/{settings.CLOUDFLARE_EMBED_MODEL}",
                                  headers={"Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}"}, json={"text": texts})
            r.raise_for_status()
        return (r.json().get("result") or {}).get("data") or []


async def embed_with_fallback(texts: list[str]) -> tuple[list[list[float]], str]:
    """(unit vectors in the same order as `texts`, the space they are in), or ([], "") when every provider failed. Never raises.
    All the texts are embedded by one model, so they can be compared with each other."""
    clean = [(t or "").strip()[:MAX_CHARS] for t in texts]
    if not clean or not all(clean):
        return [], ""

    from app.shared.llm import EMBED_MODEL, embed_text

    # Gemini embeds one text per call here; the others take the whole list in one request.
    if settings.GEMINI_API_KEY:
        vectors = []
        for t in clean:
            vector = await embed_text(t)
            if not vector:
                vectors = []
                break
            vectors.append(vector)
        if vectors:
            return vectors, f"gemini:{EMBED_MODEL}"

    await probe.refresh()
    steps = []
    if settings.MISTRAL_API_KEY:
        steps.append(("mistral", settings.MISTRAL_EMBED_MODEL, _mistral))
    if settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ACCOUNT_ID:
        steps.append(("cloudflare", settings.CLOUDFLARE_EMBED_MODEL, _cloudflare))
    known_up = [s for s in steps if not probe.is_down(f"embeddings:{s[0]}:{s[1]}")] or steps
    for name, model, call in known_up:
        try:
            with fallback_scope():
                raw = await call(clean)
            vectors = [_unit(list(v)) for v in raw]
            if len(vectors) == len(clean) and all(vectors):
                logger.info("Served these embeddings via the %s fallback.", name)
                return vectors, f"{name}:{model}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s embeddings fallback failed: %s", name, exc)
    return [], ""


def best_match(query: list[float], candidates: list[list[float]]) -> Optional[int]:
    """The position of the candidate closest to the query (dot product of unit vectors), or None when there is none."""
    if not query or not candidates:
        return None
    scores = [sum(a * b for a, b in zip(query, c)) if len(c) == len(query) else -1.0 for c in candidates]
    best = max(range(len(scores)), key=scores.__getitem__)
    return best if scores[best] > -1.0 else None
