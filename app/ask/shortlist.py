"""The few tools closest to what a person asked, so the model chooses from six to eight names and not from hundreds.

Two rankings are blended: the multilingual embeddings (a request in Tamil finds the same tool as one in English) and a plain keyword overlap. The
keywords are the floor: when no embedding service answers, the shortlist is still made, from words alone, and the answer says which was used.
Tool vectors are made once per model and kept in the database, so a restart does not embed hundreds of tools again."""
from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from typing import Awaitable, Callable, Optional

from app.db.mongo import get_db
from app.tools.contract import ToolSpec

logger = logging.getLogger(__name__)

SHORTLIST = 8
RRF_K = 20
_WORD = re.compile(r"\w+", re.UNICODE)
EmbedFn = Callable[[str, list[str]], Awaitable[list[list[float]]]]

#: space -> tool name -> (document hash, vector), loaded from the database on first use and kept
_vectors: dict[str, dict[str, tuple[str, list[float]]]] = {}
_loaded: set[str] = set()


def vectors():
    return get_db()["ask_tool_vectors"]


def document(spec: ToolSpec) -> str:
    """What a tool is, in words: its name, what it does and the ways a person might ask for it."""
    return " ".join([spec.name.replace(".", " ").replace("_", " "), spec.summary, *spec.examples])


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if len(w) > 1]


def keyword_scores(query: str, docs: dict[str, str]) -> dict[str, float]:
    """Overlap between the request's words and each tool's words, a little in favour of shorter descriptions. Zero means no word in common."""
    q = set(_words(query))
    out: dict[str, float] = {}
    for name, doc in docs.items():
        words = _words(doc)
        hits = len(q & set(words))
        out[name] = hits / math.sqrt(len(words) or 1) if hits else 0.0
    return out


def _rank(scores: dict[str, float]) -> dict[str, int]:
    return {name: i for i, (name, s) in enumerate(sorted(scores.items(), key=lambda kv: -kv[1])) if s > 0}


def fuse(*rankings: dict[str, int]) -> list[str]:
    """Reciprocal rank fusion: a tool that ranks well in either list ranks well overall."""
    total: dict[str, float] = {}
    for ranking in rankings:
        for name, position in ranking.items():
            total[name] = total.get(name, 0.0) + 1.0 / (RRF_K + position)
    return [name for name, _ in sorted(total.items(), key=lambda kv: -kv[1])]


async def _load(space: str) -> None:
    if space in _loaded:
        return
    _loaded.add(space)
    try:
        async for row in vectors().find({"space": space}):
            _vectors.setdefault(space, {})[row["name"]] = (row["hash"], row["vec"])
    except Exception as exc:  # noqa: BLE001 - the saved copy is an optimisation
        logger.debug("Could not read the saved tool vectors: %s", exc)


def _hash(doc: str) -> str:
    return hashlib.sha1(doc.encode("utf-8")).hexdigest()[:16]


async def _tool_vectors(space: str, docs: dict[str, str], embed: EmbedFn) -> Optional[dict[str, list[float]]]:
    """The vector for every tool in this space, making the ones that are missing or whose description changed. None when they cannot all be made."""
    await _load(space)
    have = _vectors.setdefault(space, {})
    stale = [n for n, d in docs.items() if n not in have or have[n][0] != _hash(d)]
    for start in range(0, len(stale), 64):
        batch = stale[start:start + 64]
        made = await embed(space, [docs[n] for n in batch])
        if len(made) != len(batch):
            return None
        for n, v in zip(batch, made):
            have[n] = (_hash(docs[n]), v)
            try:
                await vectors().update_one({"space": space, "name": n}, {"$set": {"space": space, "name": n, "hash": have[n][0], "vec": v}}, upsert=True)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not save a tool vector: %s", exc)
    return {n: have[n][1] for n in docs if n in have}


async def default_embed(space: str, texts: list[str]) -> list[list[float]]:
    from app.shared.embeddings import embed_in

    return await embed_in(space, texts)


async def shortlist(query: str, specs: list[ToolSpec], *, limit: int = SHORTLIST, embed: EmbedFn = default_embed,
                    space: Optional[str] = None) -> tuple[list[str], str]:
    """(tool names, "embeddings" or "keywords"). The embedding model is the one the caller names (the first that answers is chosen by `space_for_query`)."""
    docs = {s.name: document(s) for s in specs}
    keyword = _rank(keyword_scores(query, docs))
    ranking = None
    how = "keywords"
    if space:
        try:
            have = await _tool_vectors(space, docs, embed)
            asked = await embed(space, [query])
            if have and asked and asked[0]:
                scores = {n: sum(a * b for a, b in zip(asked[0], v)) if len(v) == len(asked[0]) else 0.0 for n, v in have.items()}
                ranking = _rank(scores)
                how = "embeddings"
        except Exception as exc:  # noqa: BLE001 - the keywords still make a shortlist
            logger.warning("The embedding shortlist failed, using keywords: %s", exc)
    names = fuse(ranking, keyword) if ranking else list(keyword)
    if not names:
        names = [s.name for s in specs[:limit]]  # nothing matched at all: the model is shown the first few and may answer "none"
    return names[:limit], how


async def pick_space() -> str:
    """The embedding model to use right now: the first that answers (Mistral, then Cloudflare, then Gemini), or "" when none does."""
    from app.shared.embeddings import available_spaces

    for space in await available_spaces():
        return space
    return ""
