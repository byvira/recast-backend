"""The short line written on a picture.

It used to be the post's first sentence cut at 90 characters, which repeated the post, ran off the picture and cut words in
half. A picture headline is a few words that name the point of the post. The model writes it from the post only (no new
numbers or claims); if that fails, the post's own first sentence is shortened at a word boundary. Never raises."""
from __future__ import annotations

import logging
import re

from app.pipelines.text.claims import unsupported_claims
from app.prompts.registry import load_prompt
from app.prompts.safe import guard_output

logger = logging.getLogger(__name__)

MAX_WORDS = 8
MAX_CHARS = 56
MIN_WORDS = 2


def _first_sentence(content: str) -> str:
    for line in (content or "").splitlines():
        line = line.strip().lstrip("#*- ").strip()
        if line:
            return re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0]
    return ""


def trim_headline(text: str, max_words: int = MAX_WORDS, max_chars: int = MAX_CHARS) -> str:
    """Shortens at a word boundary, never inside a word. Drops the trailing full stop; adds an ellipsis only when words were cut."""
    words = (text or "").replace("\n", " ").split()
    if not words:
        return ""
    kept: list[str] = []
    for word in words[:max_words]:
        if len(" ".join([*kept, word])) > max_chars:
            break
        kept.append(word)
    if not kept:  # one very long word: show it whole rather than cut it
        kept = [words[0][:max_chars]]
    cut = len(kept) < len(words)
    line = " ".join(kept).rstrip(" ,;:-")
    line = line.rstrip(".") if not cut else line
    return f"{line}…" if cut else line


def _usable(candidate: str, content: str) -> bool:
    words = candidate.split()
    if not (MIN_WORDS <= len(words) <= MAX_WORDS + 2) or len(candidate) > MAX_CHARS + 10:
        return False
    # a headline may not bring a number, price or event the post does not have
    return not unsupported_claims(candidate, [content]) if re.search(r"\d", candidate) else True


async def make_headline(content: str, language_name: str = "English") -> str:
    """A designed headline for the picture: a few words naming the point of the post."""
    content = (content or "").strip()
    fallback = trim_headline(_first_sentence(content))
    if not content:
        return fallback
    try:
        from app.shared.llm import GroqModel, call_llm

        prompt = load_prompt("media/image_headline", content=content[:2500], language_name=language_name, max_words=MAX_WORDS)
        raw = await call_llm(prompt, model=GroqModel.FAST, temperature=0.4, max_tokens=60)
        candidate = (guard_output(raw, source=prompt) or "").strip().strip('"“”\'').splitlines()[0].strip() if raw else ""
        candidate = candidate.rstrip(".")
        if candidate and _usable(candidate, content):
            return candidate
    except Exception as exc:  # noqa: BLE001
        logger.warning("Headline writing failed, shortening the post's first sentence instead: %s", exc)
    return fallback
