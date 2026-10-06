"""The per-platform settings a post can carry beyond its text and pictures (YouTube language, Instagram first comment, Facebook link,
LinkedIn visibility, Threads reply control and topic tag, Bluesky languages), and the SEO details of a Blog or Newsletter post.

A post belongs to one platform, so its settings are kept on the post (`publish_options`) and checked here when they are saved, with a
plain message for anything that is not allowed. Publishers read them from `PublishRequest.options`.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

_LANGUAGE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")
THREADS_REPLY_CONTROLS = ("everyone", "accounts_you_follow", "mentioned_only", "parent_post_author_only", "followers_only")
LINKEDIN_VISIBILITY = ("PUBLIC", "CONNECTIONS", "LOGGED_IN")


def _text(label: str, max_length: int) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{label} must be text.")
        text = value.strip()
        if len(text) > max_length:
            raise ValueError(f"{label} can be up to {max_length} characters.")
        return text

    return check


def _one_of(label: str, allowed: tuple[str, ...]) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if value not in allowed:
            raise ValueError(f"Choose one of: {', '.join(allowed)} for {label}.")
        return value

    return check


def _language(value: Any) -> str:
    if not isinstance(value, str) or not _LANGUAGE.match(value.strip()):
        raise ValueError("Use a language code such as en or en-US.")
    return value.strip()


def _languages(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 3:
        raise ValueError("Choose up to 3 languages.")
    return [_language(item) for item in value]


def _flag(label: str) -> Callable[[Any], bool]:
    def check(value: Any) -> bool:
        if not isinstance(value, bool):
            raise ValueError(f"{label} must be yes or no.")
        return value

    return check


def _https(label: str) -> Callable[[Any], str]:
    base = _text(label, 2000)

    def check(value: Any) -> str:
        text = base(value)
        if text and not re.match(r"^https://\S+$", text):
            raise ValueError(f"{label} must start with https://")
        return text

    return check


def _topic_tag(value: Any) -> str:
    text = _text("The topic tag", 50)(value)
    if "." in text or "&" in text:
        raise ValueError("A topic tag can't contain a period or an ampersand.")
    return text


_USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def _usernames(label: str, max_items: int) -> Callable[[Any], list[str]]:
    def check(value: Any) -> list[str]:
        if not isinstance(value, list) or len(value) > max_items:
            raise ValueError(f"{label} can have up to {max_items} accounts.")
        names: list[str] = []
        for item in value:
            name = item.strip().lstrip("@") if isinstance(item, str) else ""
            if not _USERNAME.match(name):
                raise ValueError(f"{label} must be Instagram usernames (letters, numbers, periods and underscores).")
            if name.lower() not in [n.lower() for n in names]:
                names.append(name)
        return names

    return check


def _digits(label: str) -> Callable[[Any], str]:
    base = _text(label, 40)

    def check(value: Any) -> str:
        text = base(value)
        if text and not text.isdigit():
            raise ValueError(f"{label} must be a number from the platform.")
        return text

    return check


#: What each platform accepts, by publish slug. Anything not listed here is refused, so a setting can never reach a platform that
#: would ignore it or fail on it.
ALLOWED: dict[str, dict[str, Callable[[Any], Any]]] = {
    "youtube": {
        "language": _language,
        "notify_subscribers": _flag("Notify subscribers"),
        "captions": _flag("Captions"),
        "playlist_id": _text("The playlist", 64),
        "thumbnail_media_id": _text("The thumbnail", 80),
    },
    "instagram": {
        "first_comment": _text("The first comment", 2200),
        "location_id": _digits("The location"),
        # The place's name, kept only so the picker can show what was chosen. It is never sent to Instagram.
        "location_name": _text("The place name", 120),
        "user_tags": _usernames("People tagged", 20),
        "collaborators": _usernames("Collaborators", 3),
    },
    "facebook": {"link": _https("The link")},
    "linkedin": {"visibility": _one_of("visibility", LINKEDIN_VISIBILITY), "link_card_url": _https("The link card address")},
    "threads": {"reply_control": _one_of("reply control", THREADS_REPLY_CONTROLS), "topic_tag": _topic_tag},
    "bluesky": {"languages": _languages, "link_card_url": _https("The link card address")},
}


def clean_options(platform: str, raw: Optional[dict]) -> dict:
    """The settings to keep for a post on `platform`. An empty value clears a setting. Raises ValueError with a plain message."""
    allowed = ALLOWED.get(platform.lower(), {})
    cleaned: dict = {}
    for key, value in (raw or {}).items():
        if key not in allowed:
            raise ValueError(f"{key.replace('_', ' ').capitalize()} isn't available for this platform.")
        if value is None or value == "" or value == []:
            continue
        cleaned[key] = allowed[key](value)
        if cleaned[key] in ("", []):
            del cleaned[key]
    return cleaned


# ── Blog and Newsletter details ──────────────────────────────────────

def _string_list(label: str, max_items: int, max_length: int) -> Callable[[Any], list[str]]:
    def check(value: Any) -> list[str]:
        if not isinstance(value, list) or len(value) > max_items:
            raise ValueError(f"{label} can have up to {max_items} items.")
        items = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{label} must be plain words.")
            if len(item.strip()) > max_length:
                raise ValueError(f"Each item in {label.lower()} can be up to {max_length} characters.")
            items.append(item.strip())
        return items

    return check


#: The details a Blog or Newsletter post carries. For a newsletter, `title` is the subject line and `meta_description` the preview line;
#: for a blog, `meta_description` is the summary.
SEO_FIELDS: dict[str, Callable[[Any], Any]] = {
    "title": _text("The title", 200),
    "meta_description": _text("The summary", 500),
    "slug": _text("The web address ending", 120),
    "primary_keyword": _text("The main keyword", 100),
    "secondary_keywords": _string_list("Keywords", 20, 100),
    "tags": _string_list("Tags", 20, 60),
    "hashtags": _string_list("Hashtags", 30, 60),
}


def clean_seo(raw: Optional[dict]) -> dict:
    """The details to merge into a post's `seo`. An empty value clears a detail. Raises ValueError with a plain message."""
    cleaned: dict = {}
    for key, value in (raw or {}).items():
        if key not in SEO_FIELDS:
            raise ValueError(f"{key.replace('_', ' ').capitalize()} can't be saved on a post.")
        cleaned[key] = SEO_FIELDS[key](value) if value is not None else None
    return cleaned
