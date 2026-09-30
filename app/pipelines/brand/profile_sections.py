"""Rules for the brand profile page: which identity fields each brand type has, how a section is
cleaned before saving, how complete a profile is, and what a word list may contain. Pure (no
database, no network), so every rule can be tested on its own.

Identity fields come in two groups. "core" fields are the ones onboarding has always stored and
the generation prompt reads. "more" fields are optional extras; they are also written into the
prompt so they really shape posts (see brand_context.jinja).
"""

from __future__ import annotations

import re
from typing import Any, Optional

SHORT_MAX = 120
LONG_MAX = 1000
LIST_MAX_ITEMS = 10
LIST_ITEM_MAX = 60
MAX_PLATFORMS = 12

COMPANY_SIZES = ["Just me", "2 to 10", "11 to 50", "51 to 200", "201 to 1,000", "More than 1,000"]
PRODUCT_STAGES = ["Idea", "Building", "Beta", "Launched", "Growing", "Mature"]


def _f(key: str, label: str, kind: str, *, required: bool = False, group: str = "core",
       options: Optional[list[str]] = None, help: str = "") -> dict:
    return {"key": key, "label": label, "kind": kind, "required": required, "group": group,
            "options": options or [], "help": help}


_BASIC = [
    _f("name", "Name", "short", required=True),
    _f("tagline", "Tagline", "short"),
    _f("description", "Description", "long", help="What it is and who it is for."),
]

IDENTITY_FIELDS: dict[str, list[dict]] = {
    "Person": [
        _f("name", "Name", "short", required=True),
        _f("profession", "What you do", "short"),
        _f("bio", "Bio", "long", help="Your background, in your own words."),
        _f("achievements", "Achievements", "list"),
        _f("headline", "Headline", "short", group="more"),
        _f("location", "Location", "short", group="more"),
        _f("website", "Website", "url", group="more"),
        _f("goals", "Goals", "list", group="more"),
    ],
    "Personal Brand": [
        _f("name", "Brand name", "short", required=True),
        _f("tagline", "Tagline", "short"),
        _f("mission", "Mission", "long"),
        _f("industry", "Industry", "short"),
        _f("niche", "Niche", "short", group="more"),
        _f("core_message", "Core message", "long", group="more"),
        _f("content_pillars", "Content pillars", "list", group="more"),
        _f("monetization", "How it earns money", "list", group="more"),
    ],
    "Business": [
        _f("company_name", "Company name", "short", required=True),
        _f("description", "What the company does", "long"),
        _f("industry", "Industry", "short"),
        _f("offerings", "Offerings", "list"),
        _f("tagline", "Tagline", "short", group="more"),
        _f("mission", "Mission", "long", group="more"),
        _f("company_size", "Company size", "select", group="more", options=COMPANY_SIZES),
        _f("target_market", "Target market", "long", group="more"),
        _f("competitors", "Competitors", "list", group="more"),
    ],
    "Product": [
        _f("product_name", "Product name", "short", required=True),
        _f("description", "What it does", "long"),
        _f("category", "Category", "short"),
        _f("use_cases", "Use cases", "list"),
        _f("one_liner", "One-liner", "short", group="more"),
        _f("problem_solved", "Problem it solves", "long", group="more"),
        _f("key_features", "Key features", "list", group="more"),
        _f("pricing", "Pricing", "short", group="more"),
        _f("stage", "Stage", "select", group="more", options=PRODUCT_STAGES),
    ],
    "Shop": _BASIC,
    "Entertainment": _BASIC,
}

READING_LEVELS = ["Simplified", "Standard", "Expert"]
KNOWLEDGE_LEVELS = ["Beginner", "Intermediate", "Advanced"]
PAIN_POINT_MAX = 500


def identity_fields(brand_type: str) -> list[dict]:
    return IDENTITY_FIELDS.get(brand_type) or _BASIC


# ---------------------------------------------------------------------------------------------
# cleaning
# ---------------------------------------------------------------------------------------------

def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit] if limit <= SHORT_MAX else str(value or "").strip()[:limit]


def normalise_url(value: str) -> Optional[str]:
    """A web address with a scheme, or None when it cannot be one. `example.com` becomes
    `https://example.com`. Anything with spaces, or without a dot in the host, is refused."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    match = re.match(r"^https?://([^/\s?#]+)(/[^\s]*)?$", raw, re.IGNORECASE)
    if not match or "." not in match.group(1) or len(raw) > 300:
        return None
    return raw


def clean_list(value: Any, *, max_items: int = LIST_MAX_ITEMS, max_chars: int = LIST_ITEM_MAX) -> list[str]:
    """Trimmed, case-insensitively unique, limited in count and length."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = " ".join(str(item or "").split())[:max_chars]
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
        if len(out) == max_items:
            break
    return out


def sanitize_identity(brand_type: str, data: dict, existing: Optional[dict] = None) -> tuple[dict, dict[str, str]]:
    """Returns (identity, errors). Only fields this brand type has are kept, each cleaned to its
    kind. Keys the page does not know about (older onboarding data) are kept as they were, so saving
    from here never throws away something stored elsewhere. `errors` maps a field key to a message."""
    fields = identity_fields(brand_type)
    known = {f["key"] for f in fields}
    out: dict = {k: v for k, v in (existing or {}).items() if k not in known}
    errors: dict[str, str] = {}
    for f in fields:
        key = f["key"]
        if key not in data:
            if key in (existing or {}):
                out[key] = existing[key]
            continue
        raw = data[key]
        kind = f["kind"]
        if kind == "short":
            value: Any = _text(raw, SHORT_MAX)
        elif kind == "long":
            value = _text(raw, LONG_MAX)
        elif kind == "url":
            value = normalise_url(str(raw or ""))
            if value is None:
                errors[key] = "Enter a web address like https://example.com."
                continue
        elif kind == "select":
            value = str(raw or "").strip()
            if value and value not in f["options"]:
                errors[key] = "Choose one of the listed options."
                continue
        else:  # list
            value = clean_list(raw)
        if f["required"] and not value:
            errors[key] = f"{f['label']} is required."
            continue
        out[key] = value
    return out, errors


def sanitize_audience(data: dict) -> tuple[dict, dict[str, str]]:
    errors: dict[str, str] = {}
    reading = str(data.get("reading_level") or "Standard")
    knowledge = str(data.get("knowledge_base") or "Intermediate")
    if reading not in READING_LEVELS:
        errors["reading_level"] = "Choose one of the listed reading levels."
    if knowledge not in KNOWLEDGE_LEVELS:
        errors["knowledge_base"] = "Choose one of the listed levels."
    pain = " ".join(str(data.get("primary_pain_point") or "").split())[:PAIN_POINT_MAX]
    return {"reading_level": reading, "knowledge_base": knowledge, "primary_pain_point": pain,
            "extra": data.get("extra") if isinstance(data.get("extra"), dict) else {}}, errors


def sanitize_platforms(value: Any) -> tuple[list[str], dict[str, str]]:
    if not isinstance(value, list):
        return [], {"platforms": "Platforms must be a list."}
    out: list[str] = []
    for item in value:
        slug = str(item or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_+\-/ ]{1,30}", slug):
            return [], {"platforms": f"'{slug[:20]}' is not a valid platform."}
        if slug not in out:
            out.append(slug)
    if len(out) > MAX_PLATFORMS:
        return [], {"platforms": f"Choose at most {MAX_PLATFORMS} platforms."}
    return out, {}


def resolve_default_platform(platforms: list[str], requested: Any, existing: Any) -> Optional[str]:
    """The one platform a brand posts to by default. It must be one of the brand's platforms: the
    requested one if valid, else the one already set if still valid, else the first, else none."""
    for candidate in (requested, existing):
        if isinstance(candidate, str) and candidate in platforms:
            return candidate
    return platforms[0] if platforms else None


# ---------------------------------------------------------------------------------------------
# word lists
# ---------------------------------------------------------------------------------------------

MAX_OPENERS = 15
MAX_BANNED = 50
MAX_SYNONYMS = 50
PHRASE_PLACEMENTS = ["hook", "transition", "close", "any"]


def validate_words(manual: dict) -> tuple[dict, dict[str, str]]:
    """Cleans the openers, closers, banned words and synonym pairs, and refuses a word that is both
    banned and a preferred replacement. Pairs are {original, replacement}. Duplicates are dropped quietly. Phrases are left as given."""
    errors: dict[str, str] = {}
    out = dict(manual)
    out["openers"] = clean_list(manual.get("openers"), max_items=MAX_OPENERS, max_chars=160)
    out["closers"] = clean_list(manual.get("closers"), max_items=MAX_OPENERS, max_chars=160)
    out["banned_words"] = clean_list(manual.get("banned_words"), max_items=MAX_BANNED, max_chars=60)

    pairs: list[dict] = []
    seen: set[str] = set()
    for pair in manual.get("preferred_synonyms") or []:
        if not isinstance(pair, dict):
            continue
        original = " ".join(str(pair.get("original") or "").split())[:60]
        replacement = " ".join(str(pair.get("replacement") or "").split())[:60]
        if original and replacement and original.lower() not in seen:
            seen.add(original.lower())
            pairs.append({"original": original, "replacement": replacement})
        if len(pairs) == MAX_SYNONYMS:
            break
    out["preferred_synonyms"] = pairs

    banned = {w.lower() for w in out["banned_words"]}
    clash = sorted({p["replacement"] for p in pairs if p["replacement"].lower() in banned})
    if clash:
        errors["preferred_synonyms"] = f"{', '.join(clash)} is on your banned list, so it can't be a replacement."
    return out, errors


# ---------------------------------------------------------------------------------------------
# completeness
# ---------------------------------------------------------------------------------------------

SECTION_ORDER = ["identity", "audience", "voice", "words", "samples", "visual", "onboarding", "platforms"]


def compute_completeness(doc: dict) -> dict:
    """How filled in a profile is, per section, worked out on the server so every screen agrees.
    Returns {percent, done, total, sections: {name: bool}, first_incomplete}."""
    brand_type = doc.get("brand_type") or ""
    identity = doc.get("identity") or {}
    core = [f for f in identity_fields(brand_type) if f["group"] == "core"]
    filled_core = [f for f in core if identity.get(f["key"]) not in (None, "", [])]
    required_ok = all(identity.get(f["key"]) for f in core if f["required"])

    audience = doc.get("audience") or {}
    voice = doc.get("voice_tone") or {}
    manual = doc.get("manual_data") or {}
    visual = doc.get("visual_identity") or {}
    colors = visual.get("colors") or {}
    fonts = visual.get("fonts") or {}

    sections = {
        "identity": required_ok and len(filled_core) >= min(3, len(core)),
        "audience": bool((audience.get("primary_pain_point") or "").strip()),
        "voice": bool(voice.get("tones")) or bool((voice.get("style") or "").strip()),
        "words": any(manual.get(k) for k in ("openers", "closers", "phrases", "banned_words", "preferred_synonyms")),
        "samples": len(doc.get("training_samples") or []) > 0,
        "visual": bool(visual.get("logo_url")) or any(colors.get(k) for k in ("primary", "secondary", "accent"))
        or any(fonts.get(k) for k in ("heading", "body")),
        "onboarding": bool(doc.get("is_complete")) or any(doc.get(k) for k in ("pillars_data", "icp_data", "positioning_data")),
        "platforms": len(doc.get("platforms") or []) > 0,
    }
    done = sum(1 for v in sections.values() if v)
    first = next((name for name in SECTION_ORDER if not sections[name]), None)
    return {
        "percent": round(100 * done / len(SECTION_ORDER)),
        "done": done,
        "total": len(SECTION_ORDER),
        "sections": sections,
        "first_incomplete": first,
    }


def onboarding_field_for(brand_type: str) -> Optional[str]:
    """Which stored field holds this brand type's onboarding answers (pillars, customer, positioning)."""
    return {"Personal Brand": "pillars_data", "Business": "icp_data", "Product": "positioning_data"}.get(brand_type)
