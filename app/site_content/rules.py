"""What each kind of website content holds, the limits on every field, and the checks a change must pass before it goes live."""
from __future__ import annotations

import math
import re
from datetime import date
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.site_content.html_clean import clean_html, plain_text

Kind = Literal["post", "job", "page"]
# the pages edited from Ops; everything else on the website stays in code
PAGES = ("pricing", "faq", "privacy", "terms", "data-deletion")
LEGAL = ("privacy", "terms", "data-deletion")
PLANS = ("free", "single", "duo", "large")
SLUG = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
HTML_MAX = 400_000


class Intro(BaseModel):
    eyebrow: str = Field("", max_length=40)
    title: str = Field("", max_length=90)
    description: str = Field("", max_length=260)


class QA(BaseModel):
    id: str = Field(..., min_length=1, max_length=40)
    question: str = Field("", max_length=160)
    answer: str = Field("", max_length=700)


class QAGroup(BaseModel):
    id: str = Field(..., min_length=1, max_length=40)
    label: str = Field("", max_length=60)
    items: list[QA] = Field(default_factory=list, max_length=30)


class PostData(BaseModel):
    title: str = Field("", max_length=140)
    summary: str = Field("", max_length=300)
    slug: str = Field("", max_length=80)
    author: str = Field("", max_length=80)
    cover_url: str = Field("", max_length=600)
    cover_alt: str = Field("", max_length=200)
    html: str = Field("", max_length=HTML_MAX)
    seo_title: str = Field("", max_length=70)
    seo_description: str = Field("", max_length=170)

    @field_validator("html")
    @classmethod
    def _clean(cls, v: str) -> str:
        return clean_html(v)

    @field_validator("cover_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if v and not v.startswith("https://"):
            raise ValueError("Use a picture uploaded here, or an address that starts with https://")
        return v


class JobData(BaseModel):
    title: str = Field("", max_length=120)
    summary: str = Field("", max_length=300)
    slug: str = Field("", max_length=80)
    team: str = Field("", max_length=60)
    place: str = Field("", max_length=80)
    job_type: Literal["full_time", "part_time", "contract", "internship"] = "full_time"
    pay: str = Field("", max_length=80)
    apply_email: str = Field("", max_length=200)
    apply_url: str = Field("", max_length=600)
    closes_on: str = Field("", max_length=10)
    html: str = Field("", max_length=HTML_MAX)

    @field_validator("html")
    @classmethod
    def _clean(cls, v: str) -> str:
        return clean_html(v)

    @field_validator("apply_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if v and not v.startswith("https://"):
            raise ValueError("The apply link must start with https://")
        return v

    @field_validator("closes_on")
    @classmethod
    def _date(cls, v: str) -> str:
        if v:
            date.fromisoformat(v)
        return v


class PricingData(BaseModel):
    intro: Intro = Field(default_factory=Intro)
    blurbs: dict[Literal["free", "single", "duo", "large"], str] = Field(default_factory=dict)
    featured: Literal["", "free", "single", "duo", "large"] = "duo"
    credit_note: str = Field("", max_length=260)
    contact_label: str = Field("", max_length=80)
    faq: list[QA] = Field(default_factory=list, max_length=20)

    @field_validator("blurbs")
    @classmethod
    def _blurbs(cls, v: dict[str, str]) -> dict[str, str]:
        for text in v.values():
            if len(text) > 200:
                raise ValueError("Each plan's words fit in 200 characters.")
        return v


class FaqData(BaseModel):
    intro: Intro = Field(default_factory=Intro)
    groups: list[QAGroup] = Field(default_factory=list, max_length=12)


class LegalData(BaseModel):
    title: str = Field("", max_length=80)
    effective: str = Field("", max_length=10)
    html: str = Field("", max_length=HTML_MAX)

    @field_validator("html")
    @classmethod
    def _clean(cls, v: str) -> str:
        return clean_html(v)

    @field_validator("effective")
    @classmethod
    def _date(cls, v: str) -> str:
        if v:
            date.fromisoformat(v)
        return v


def model_for(kind: str, key: str) -> type[BaseModel]:
    if kind == "post":
        return PostData
    if kind == "job":
        return JobData
    if key == "pricing":
        return PricingData
    if key == "faq":
        return FaqData
    return LegalData


def clean(kind: str, key: str, data: dict[str, Any]) -> dict[str, Any]:
    """The draft as it is stored: every field in its shape and limits, rich text cleaned."""
    return model_for(kind, key).model_validate(data).model_dump()


def title_of(kind: str, key: str, data: dict[str, Any]) -> str:
    if kind in ("post", "job"):
        return (data.get("title") or "").strip() or "Untitled"
    return {"pricing": "Pricing page", "faq": "Questions page", "privacy": "Privacy", "terms": "Terms",
            "data-deletion": "Data deletion"}[key]


def read_minutes(html: str) -> int:
    return max(1, math.ceil(len(plain_text(html).split()) / 220))


_DASH = re.compile("[—–]")


def _texts(kind: str, key: str, data: dict[str, Any]) -> list[tuple[str, str]]:
    """Every piece of words a visitor reads, with the name of its field, for the copy checks."""
    out: list[tuple[str, str]] = []
    if kind in ("post", "job", "page") and "html" in data:
        out.append(("Text", plain_text(data.get("html") or "")))
    for name in ("title", "summary", "seo_title", "seo_description", "credit_note", "contact_label", "team", "place", "pay"):
        if data.get(name):
            out.append((name.replace("_", " ").capitalize(), data[name]))
    intro = data.get("intro") or {}
    for name in ("eyebrow", "title", "description"):
        if intro.get(name):
            out.append((f"Top of the page, {name}", intro[name]))
    for plan, text in (data.get("blurbs") or {}).items():
        out.append((f"{plan.capitalize()} plan words", text))
    for qa in data.get("faq") or []:
        out.append(("A question", f"{qa.get('question', '')} {qa.get('answer', '')}"))
    for group in data.get("groups") or []:
        out.append(("A group name", group.get("label", "")))
        for qa in group.get("items") or []:
            out.append(("A question", f"{qa.get('question', '')} {qa.get('answer', '')}"))
    return out


def problems(kind: str, key: str, data: dict[str, Any]) -> list[dict[str, str]]:
    """What stops this change going live, in plain words. Empty when it is ready."""
    found: list[dict[str, str]] = []

    def need(field: str, ok: bool, message: str) -> None:
        if not ok:
            found.append({"field": field, "message": message})

    if kind in ("post", "job"):
        need("title", bool((data.get("title") or "").strip()), "Add a title.")
        need("slug", bool(re.match(SLUG, data.get("slug") or "")), "Add a web address with small letters, numbers and dashes only.")
        need("summary", bool((data.get("summary") or "").strip()), "Add a short summary. It shows on the list and in search results.")
        need("html", len(plain_text(data.get("html") or "").split()) >= 20, "Write at least a few lines.")
    if kind == "post":
        need("cover_alt", not data.get("cover_url") or bool((data.get("cover_alt") or "").strip()),
             "Describe the cover picture for people who can't see it.")
    if kind == "job":
        need("apply", bool(data.get("apply_email") or data.get("apply_url")), "Add an email address or a link to apply.")
        need("closes_on", not data.get("closes_on") or date.fromisoformat(data["closes_on"]) >= date.today(),
             "The closing date has passed. Choose a later date.")
    if kind == "page" and key == "pricing":
        need("intro", bool((data.get("intro") or {}).get("title", "").strip()), "Add a headline.")
        for plan in PLANS:
            need(f"blurbs.{plan}", bool(((data.get("blurbs") or {}).get(plan) or "").strip()), f"Add the words for {plan.capitalize()}.")
        for qa in data.get("faq") or []:
            need("faq", bool(qa.get("question", "").strip() and qa.get("answer", "").strip()), "Every question needs an answer.")
    if kind == "page" and key == "faq":
        need("intro", bool((data.get("intro") or {}).get("title", "").strip()), "Add a headline.")
        for group in data.get("groups") or []:
            need("groups", bool(group.get("label", "").strip()), "Every group needs a name.")
            for qa in group.get("items") or []:
                need("groups", bool(qa.get("question", "").strip() and qa.get("answer", "").strip()), "Every question needs an answer.")
    if kind == "page" and key in LEGAL:
        need("title", bool((data.get("title") or "").strip()), "Add a title.")
        need("effective", bool(data.get("effective")), "Add the date this version takes effect.")
        need("html", len(plain_text(data.get("html") or "").split()) >= 20, "Write the page.")
    for field, text in _texts(kind, key, data):
        if _DASH.search(text or ""):
            found.append({"field": field, "message": f"{field}: use a comma, a full stop or brackets instead of a long dash."})
    seen: set[str] = set()
    unique = []
    for p in found:
        sig = p["field"] + p["message"]
        if sig not in seen:
            seen.add(sig)
            unique.append(p)
    return unique


def slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (text or "").lower())).strip("-")[:80]


def is_open(job: dict[str, Any], today: Optional[date] = None) -> bool:
    closes = job.get("closes_on")
    return not closes or date.fromisoformat(closes) >= (today or date.today())
