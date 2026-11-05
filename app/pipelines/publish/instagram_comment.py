"""An Instagram first comment written from the post's caption, on request.

Brands often put their closing hashtags or one more useful line in the first comment, posted right after the post goes live. This
writes one starting point from the real caption and the brand's name. It returns text only: a person reads it, changes it and
decides whether it goes out. Nothing is posted from here.

It is one small model call, never repeats the caption, keeps the caption's own closing hashtags (the same ones the "Move hashtags"
button would move), and says plainly when it cannot make one."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

from app.db.mongo import get_db
from app.shared.llm import GroqModel, call_llm

logger = logging.getLogger(__name__)

#: Instagram's own limit for a comment.
MAX_COMMENT_CHARS = 2200
#: What the writer is asked to stay inside: a first comment reads best short.
ASK_CHARS = 400
CAPTION_CHARS = 2200
#: How many a member may ask for in a day, so the free model allowance is shared fairly.
DAILY_CAP = 30

_TRAILING_TAGS = re.compile(r"(?:\s+#[^\s#]+)+\s*$")
_ONLY_TAGS = re.compile(r"(?:#[^\s#]+\s*)+")
_DASHES = re.compile(r"\s*[—–]\s*")
_FENCE = re.compile(r"^```[a-z]*\n?|```$")
_LABEL = re.compile(r"^(?:first\s+)?comment\s*:\s*", re.IGNORECASE)


def split_hashtags(caption: str) -> tuple[str, str]:
    """The caption without its closing run of hashtags, and those hashtags (empty when there are none)."""
    text = (caption or "").strip()
    match = _TRAILING_TAGS.search(text)
    if not match or match.start() == 0:
        return text, ""
    body = text[: match.start()].rstrip()
    if _ONLY_TAGS.fullmatch(body):
        return text, ""  # a caption that is nothing but hashtags has no words to write a comment about
    return body, match.group(0).strip()


def clean_comment(raw: str) -> str:
    """The model's answer made ready to show: no code fence, label or surrounding quotes, no dash punctuation, within the limit."""
    text = _FENCE.sub("", (raw or "").strip()).strip()
    text = _LABEL.sub("", text).strip().strip('"“”').strip()
    text = _DASHES.sub(", ", text)
    if len(text) <= MAX_COMMENT_CHARS:
        return text
    cut = max(text.rfind(mark, 0, MAX_COMMENT_CHARS) for mark in (". ", "! ", "? ", "\n"))
    return (text[: cut + 1] if cut > MAX_COMMENT_CHARS // 2 else text[:MAX_COMMENT_CHARS]).strip()


def repeats_caption(comment: str, caption: str) -> bool:
    """True when the comment is just the caption again (or most of it), which adds nothing under the post."""
    a = " ".join(re.sub(r"[#\W_]+", " ", comment.lower()).split())
    b = " ".join(re.sub(r"[#\W_]+", " ", caption.lower()).split())
    if not a:
        return True
    if a == b or (len(a) > 20 and a in b) or (len(b) > 20 and b in a):
        return True
    # a rewording that keeps almost every word is still the caption again
    words, theirs = a.split(), set(b.split())
    return len(words) >= 5 and sum(1 for w in words if w in theirs) / len(words) >= 0.8


async def allowed_today(user_id: str) -> bool:
    """Counts one ask against the member's daily allowance; False once it is used up."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = await get_db()["first_comment_usage"].find_one_and_update(
        {"user_id": user_id, "date": today}, {"$inc": {"count": 1}, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
        upsert=True, return_document=True,
    )
    return int((row or {}).get("count", 1)) <= DAILY_CAP


async def suggest_first_comment(caption: str, brand_name: Optional[str]) -> str:
    """One first comment for this caption. Raises HTTPException with a plain message when none can be made."""
    from app.prompts.registry import load_prompt

    body, hashtags = split_hashtags(caption[:CAPTION_CHARS])
    if len(body.strip()) < 10:
        raise HTTPException(status_code=400, detail="Write the caption first, then ask for a first comment.")
    prompt = load_prompt("publish/instagram_first_comment", brand_name=brand_name or "this brand", caption=body, hashtags=hashtags, max_chars=ASK_CHARS)
    try:
        raw = await call_llm(prompt, model=GroqModel.FAST, temperature=0.7, max_tokens=260)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - a busy or unavailable model is a plain message, never a crash
        logger.warning("First comment could not be written: %s", exc)
        raise HTTPException(status_code=503, detail="Couldn't write one right now. You can type the comment yourself.")
    comment = clean_comment(raw)
    if hashtags and hashtags.lower() not in comment.lower():
        # the caption's own closing hashtags always ride along, even when the writer left them out
        comment = f"{comment}\n{hashtags}".strip()
    if not comment or repeats_caption(comment.replace(hashtags, ""), body):
        raise HTTPException(status_code=502, detail="That came back too close to your caption. Try again.")
    return comment[:MAX_COMMENT_CHARS]
