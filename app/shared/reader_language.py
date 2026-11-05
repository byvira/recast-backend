"""Which language a person reads, and saying something to them in it.

One place for every message that goes to a person: push notifications, emails and the plain error messages the API returns. Each asks
for the reader of one `audience`, the order of languages that audience is read in is written once here, and `say` turns an English
sentence into that language through the translation store the rest of the product already uses (translated once, remembered).

Audiences:
  member     one person about their own work: their language, then the workspace's, then English (Remy, push, errors)
  workspace  the workspace as a group: its language, then its owner's, then English (Odette, workspace emails)
  owner      the workspace owner: their own language, then the workspace's, then English (billing and account emails)
  content    what is written for the audience of a post: asked for, then piece, campaign, brand, workspace, detected, member, English

Anything that goes wrong keeps the English: a message is never lost or left blank because a translation did not happen.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

Audience = Literal["member", "workspace", "owner", "content"]
TRANSLATE_SECONDS = 2.5
ERROR_MAX_CHARS = 160
CACHE_SECONDS = 60
CACHE_MAX = 2000

_cache: dict[tuple, tuple[float, str]] = {}


def _base(code: Optional[str]) -> str:
    return (code or "").strip().lower().split("+")[0].split("-")[0].split("_")[0]


def wants_english(language: Optional[str]) -> bool:
    """English, nothing chosen, or a blend such as Tanglish, which is read in English: the screens are English."""
    code = (language or "").strip().lower()
    return not code or "+" in code or _base(code) == "en"


async def reader_language(
    audience: Audience, *, user_id: Optional[str] = None, workspace_id: Optional[str] = None,
    explicit: Optional[str] = None, text: Optional[str] = None,
) -> str:
    """The language this reader should be written to in. Never raises; English when nothing is known."""
    key = (audience, user_id, workspace_id, explicit)
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic() and not text:
        return hit[1]
    try:
        language = await _resolve(audience, user_id, workspace_id, explicit, text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reader_language failed for %s: %s", audience, exc)
        language = "en"
    if not text:
        if len(_cache) >= CACHE_MAX:
            _cache.clear()
        _cache[key] = (time.monotonic() + CACHE_SECONDS, language)
    return language


async def _resolve(audience: str, user_id: Optional[str], workspace_id: Optional[str], explicit: Optional[str], text: Optional[str]) -> str:
    from app.shared import language as chain

    if explicit:
        return explicit
    if audience == "member":
        return chain.first_present(await chain.user_language(user_id), await chain.workspace_language(workspace_id))
    if audience == "owner":
        owner_id = await _owner_of(workspace_id)
        return chain.first_present(await chain.user_language(owner_id or user_id), await chain.workspace_language(workspace_id))
    if audience == "workspace":
        owner_id = await _owner_of(workspace_id)
        return chain.first_present(await chain.workspace_language(workspace_id), await chain.user_language(owner_id))
    return await chain.resolve_content_language(workspace_id=workspace_id, user_id=user_id, text=text)


async def _owner_of(workspace_id: Optional[str]) -> Optional[str]:
    if not workspace_id:
        return None
    from app.db.mongo import workspaces

    doc = await workspaces.find_one({"id": workspace_id}, {"owner_id": 1}) or {}
    return doc.get("owner_id")


def forget() -> None:
    _cache.clear()


async def say(key: str, english: str, language: Optional[str], ctx: Optional[dict[str, Any]] = None, *, wait: float = TRANSLATE_SECONDS) -> str:
    """`english` in `language`, with `{slots}` filled from `ctx`. English is returned without a model call. If the translation is not
    ready within `wait` seconds the English is returned now and the translation keeps going, so it is ready the next time."""
    filled = _fill(english, ctx)
    if wants_english(language):
        return filled
    from app.shared.localized_strings import get_localized_string

    task = asyncio.ensure_future(get_localized_string(key, _base(language), english, ctx))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=wait)
    except asyncio.TimeoutError:
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return filled
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not say %s in %s: %s", key, language, exc)
        return filled


def _fill(template: str, ctx: Optional[dict[str, Any]]) -> str:
    try:
        return template.format(**(ctx or {}))
    except (KeyError, IndexError, ValueError):
        return template


_HAS_DIGIT_OR_ID = re.compile(r"[\d{}\"'`<>@/\\]")


def is_static_message(detail: Any) -> bool:
    """A plain sentence that reads the same every time, so one translation serves every reader. A message with a number, a name, a
    link or an id in it is different each time and is left in English."""
    return isinstance(detail, str) and 3 <= len(detail) <= ERROR_MAX_CHARS and not _HAS_DIGIT_OR_ID.search(detail)


async def say_error(detail: Any, language: Optional[str]) -> Any:
    """An error message in the reader's language when it is a plain sentence; otherwise exactly as it was."""
    if wants_english(language) or not is_static_message(detail):
        return detail
    import hashlib

    key = f"error.{hashlib.sha1(detail.encode('utf-8')).hexdigest()[:20]}"
    return await say(key, detail.replace("{", "{{").replace("}", "}}"), language, wait=1.5)


# ---- email templates: use the language's own template when one has been published, otherwise English --------------------------------------
_template_names: tuple[float, frozenset[str]] = (0.0, frozenset())
TEMPLATE_SECONDS = 600


async def published_templates() -> frozenset[str]:
    """The aliases of the email templates published on Resend, remembered for ten minutes. Unknown (no key, or Resend unreachable) is empty."""
    global _template_names
    now = time.monotonic()
    if _template_names[0] > now:
        return _template_names[1]
    names: frozenset[str] = frozenset()
    try:
        from app.core.config import settings

        if settings.RESEND_API_KEY and settings.ENVIRONMENT == "production":
            import httpx

            async with httpx.AsyncClient(timeout=4) as client:
                res = await client.get("https://api.resend.com/templates", headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"})
            if res.status_code == 200:
                data = res.json().get("data") or []
                names = frozenset(str(t.get("alias") or t.get("id") or "") for t in data) - {""}
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not list the email templates: %s", exc)
    _template_names = (now + TEMPLATE_SECONDS, names)
    return names


async def template_for(alias: str, language: Optional[str], published: Optional[frozenset[str]] = None) -> str:
    """The template to send: `alias-hi` for a Hindi reader when that template exists, otherwise `alias`. Add a template named like
    that on Resend and the next email in that language uses it, with no change here."""
    if wants_english(language):
        return alias
    names = published if published is not None else await published_templates()
    candidate = f"{alias}-{_base(language)}"
    return candidate if candidate in names else alias
