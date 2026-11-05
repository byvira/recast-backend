"""Safety profiles a brand can choose, each adding care on top of the standard checks.

A profile can only add: stricter wording checks, more blocked terms, and a policy note that the model read is given. Ops can edit a
profile's extra terms and note, and turn strict on, but nothing here can loosen the standard checks or switch a profile's model read
off. A brand with no profile uses `general`, which adds nothing.
"""
from __future__ import annotations

from typing import Any, Optional

DEFAULT_PROFILE = "general"

PROFILES: dict[str, dict[str, Any]] = {
    "general": {
        "label": "General", "description": "Everyday brands. The standard checks and nothing extra.",
        "strict": False, "terms": [], "note": "",
    },
    "kids": {
        "label": "Children and families", "description": "Brands that speak to children or families. Stricter wording, and no asking a child for personal details.",
        "strict": True, "terms": [],
        "note": "The audience includes children. Anything frightening, adult, or asking a child for personal details, a photo or money is unsafe.",
    },
    "health": {
        "label": "Health and wellness", "description": "Clinics, supplements, fitness and wellbeing. No claims to treat, cure or prevent disease.",
        "strict": False, "terms": [],
        "note": "A claim that a product treats, cures or prevents a disease, replaces medical advice, or promises a health result is unsafe. General wellbeing language is fine.",
    },
    "finance": {
        "label": "Finance and investing", "description": "Banks, loans, trading and investing. No promised returns or certainty about the market.",
        "strict": False, "terms": [],
        "note": "Promised or hinted guaranteed or high returns, investment tips presented as certain, and loans or credit offered without clear terms are unsafe.",
    },
    "restricted": {
        "label": "Alcohol, gambling and tobacco", "description": "Products with an age limit. Posts must make clear they are for adults of legal age.",
        "strict": True, "terms": [],
        "note": "Promoting alcohol, gambling, tobacco or vaping to the public is unsafe unless the post clearly says it is for adults of legal age.",
    },
    "news": {
        "label": "News and politics", "description": "Publishers and public voices. Reporting violence is fine; spreading unsourced claims as fact is not.",
        "strict": False, "terms": [],
        "note": "Calls to violence, claims about elections or public figures stated as fact without a source, and content meant to mislead are unsafe. Reporting on violence is fine.",
    },
}
NOTE_MAX = 400
TERM_LIMIT = 40


def known(key: Optional[str]) -> str:
    return key if key in PROFILES else DEFAULT_PROFILE


def tidy_overrides(raw: Any) -> dict[str, dict[str, Any]]:
    """Ops' edits, kept for known profiles only: extra terms, a note, and strict. Anything else is dropped."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        if key not in PROFILES or key == DEFAULT_PROFILE or not isinstance(value, dict):
            continue
        terms: list[str] = []
        for t in value.get("terms") or []:
            term = str(t).strip().lower()[:80]
            if term and term not in terms:
                terms.append(term)
        entry = {"terms": terms[:TERM_LIMIT * 5], "note": " ".join(str(value.get("note") or "").split())[:NOTE_MAX], "strict": bool(value.get("strict"))}
        if entry["terms"] or entry["note"] or entry["strict"]:
            out[key] = entry
    return out


def effective(cfg: dict[str, Any], profile: Optional[str]) -> dict[str, Any]:
    """The settings with this profile's care added. Stricter, never looser: strict stays strict, terms are added, nothing is removed."""
    key = known(profile)
    if key == DEFAULT_PROFILE:
        return cfg
    base = PROFILES[key]
    mine = (cfg.get("profiles") or {}).get(key) or {}
    strict = bool(base["strict"] or mine.get("strict") or cfg.get("strictness") == "strict")
    terms = [*(cfg.get("extra_blocked_terms") or []), *base["terms"], *(mine.get("terms") or [])]
    return {**cfg, "strictness": "strict" if strict else cfg.get("strictness", "standard"), "extra_blocked_terms": list(dict.fromkeys(terms))}


def note(cfg: dict[str, Any], profile: Optional[str]) -> str:
    """The policy sentence the model read is given for this profile: Ops' own wording when set, otherwise the starting one."""
    key = known(profile)
    if key == DEFAULT_PROFILE:
        return ""
    mine = ((cfg.get("profiles") or {}).get(key) or {}).get("note")
    return mine or PROFILES[key]["note"]


def always_read(profile: Optional[str]) -> bool:
    """A profile other than general always gets the model read, when model checks are on at all."""
    return known(profile) != DEFAULT_PROFILE


def catalogue(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """What the Ops screen shows: each profile's starting values next to Ops' edits."""
    out = []
    for key, base in PROFILES.items():
        mine = (cfg.get("profiles") or {}).get(key) or {}
        out.append({
            "key": key, "label": base["label"], "description": base["description"], "default_strict": base["strict"], "default_note": base["note"],
            "strict": bool(base["strict"] or mine.get("strict")), "terms": mine.get("terms") or [], "note": mine.get("note") or "",
        })
    return out
