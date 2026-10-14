"""The words on a cover: a few short options written from the material, each checked against the platform's room for words."""
from __future__ import annotations

import re
from typing import Any, Optional

from app.video.analysis import analyze as A
from app.video.covers.platforms import platform as platform_row

_SPACE = re.compile(r"\s+")


def clean_line(text: Any, max_chars: int) -> str:
    t = _SPACE.sub(" ", str(text or "")).strip().strip('"“”')
    return t[:max_chars].rstrip(" ,;:-")


def fallback(title: str, max_chars: int) -> str:
    """A title cut at a word, never in the middle of one."""
    t = _SPACE.sub(" ", (title or "").strip())
    if len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    return cut.rsplit(" ", 1)[0].rstrip(" ,;:-") if " " in cut else cut


def validate_options(raw: Any, max_chars: int, max_lines: int, count: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in (raw.get("options") if isinstance(raw, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        text = clean_line(item.get("text"), max_chars)
        if len(text) < 3 or text.casefold() in seen:
            continue
        # at the size words are drawn at a line holds only a few words, so more words than the lines can hold are refused
        if len(text.split(" ")) > max_lines * 6:
            continue
        accent = clean_line(item.get("accent"), 40)
        if accent and accent.casefold() not in text.casefold():
            accent = ""
        seen.add(text.casefold())
        out.append({"text": text, "accent": accent})
        if len(out) >= count:
            break
    return out


async def cover_options(
    *, platform_id: str, material: str, title: str, language: str = "en", count: int = 3, caller: Optional[A.Caller] = None,
) -> tuple[list[dict[str, str]], bool]:
    """(options, written_by_model). Always returns at least one option when there is a title: when the model is unavailable or
    its answer does not pass the checks, the title is used, cut at a word."""
    row = platform_row(platform_id)
    fallback_option = [{"text": fallback(title, row["max_chars"]), "accent": ""}] if title.strip() else []
    if not material.strip():
        return fallback_option, False
    try:
        raw, _ = await A.cached_call(
            "video/cover_text",
            {"platform_label": row["label"], "language": language, "max_chars": row["max_chars"], "max_lines": row["max_lines"],
             "count": count, "material": material[:3000]},
            caller=caller or A._default_caller, pacer=A.Pacer(),
        )
    except Exception:  # noqa: BLE001 - a missing model never blocks a cover
        return fallback_option, False
    options = validate_options(raw, row["max_chars"], row["max_lines"], count)
    return (options, True) if options else (fallback_option, False)
