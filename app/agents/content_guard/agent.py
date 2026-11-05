"""Content Guard: a standalone agent that checks a piece of text and puts it right before it reaches anyone.

Steps, in order:
  1. Clean: dashes, invisible characters, chat preambles and filler wording are removed (no model call).
  2. Screen: free rules check for adult, hateful, violent, self harm and assistant-leftover text, plus Ops's own terms.
  3. Model check (only when Ops turns it on): a small model reads the text for what the rules cannot see.
  4. Rewrite: when something is flagged and rewriting is on, one model call rewrites it without the problem, the
     result is cleaned and screened again.
  5. Verdict: "clean" (nothing to change), "fixed" (cleanup only), "rewritten", or "blocked" (still unsafe, never to be sent).

Every blocked or rewritten text is recorded for Ops. A normal piece costs no model call.
"""

from __future__ import annotations

import hashlib
from contextvars import ContextVar
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Iterable, Any, Optional

from app.agents.content_guard import config as guard_config
from app.core.config import settings
from app.agents.content_guard import judges
from app.agents.content_guard import ladder
from app.agents.content_guard import profiles
from app.agents.content_guard import term_lists
from app.shared.language_profile import profile as language_profile
from app.agents.content_guard.rules import CATEGORIES, ScreenResult, ai_phrases_in, clean_text, content_hash, risk_signals, screen_text
from app.agents.content_guard.store import record_event

logger = logging.getLogger(__name__)

OUTCOMES = ("clean", "fixed", "rewritten", "blocked")
MAX_CHECK_CHARS = 6000
MAX_CHECK_PIECES = 4
_model_verdicts: "OrderedDict[str, ScreenResult]" = OrderedDict()
_MODEL_CACHE = 500


@dataclass
class GuardResult:
    text: str
    outcome: str = "clean"
    categories: list[str] = field(default_factory=list)
    matches: list[str] = field(default_factory=list)
    ai_phrases: list[str] = field(default_factory=list)
    message: str = ""
    # A model's finding that is only passed on as a note, because its level for that kind of problem is Advise
    notes: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.outcome == "blocked"

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "outcome": self.outcome,
            "categories": self.categories,
            "category_names": [CATEGORIES.get(c, c) for c in self.categories],
            "matches": self.matches,
            "ai_phrases": self.ai_phrases,
            "message": self.message,
            "notes": self.notes,
        }


def _wants_model(text: str, cfg: dict[str, Any], stage: str, language: Optional[str] = None, profile: Optional[str] = None) -> bool:
    """Whether a model should read this text: always for a post about to go out when Ops chose "publish", for any text with risk
    signals (a listed bad word or a sensitive topic) when Ops chose "risky" or "publish", and for text the English-wording rules
    cannot read (another script, or a language other than English, asked for or found) whenever a model check is on."""
    mode = cfg.get("model_check", "off")
    if mode == "off" or not settings.CONTENT_GUARD_LIVE_CHECKS:
        return False
    if mode == "publish" and stage == "publish":
        return True
    if profiles.always_read(profile):
        return True
    if language_profile(text, language).regional:
        return True
    return bool(risk_signals(text))


def rule_screen(text: str, settings: Optional[dict[str, Any]] = None, own_contacts: Iterable[str] = (), profile: Optional[str] = None, language: Optional[str] = None) -> ScreenResult:
    """The free rule screen with the current Ops settings. Synchronous, so the publish gate can use it."""
    cfg = term_lists.apply(profiles.effective(settings or guard_config.current(), profile), text, language)
    if not cfg.get("enabled", True):
        return ScreenResult()
    return screen_text(
        text,
        strictness=cfg.get("strictness", "standard"),
        extra_blocked_terms=cfg.get("extra_blocked_terms", []),
        allowed_terms=cfg.get("allowed_terms", []),
        own_contacts=own_contacts,
    )


_contact_cache: dict[str, tuple[float, tuple[frozenset[str], str, Optional[str]]]] = {}
_CONTACT_SECONDS = 60


async def brand_policy(brand_id: Optional[str]) -> tuple[frozenset[str], str, Optional[str]]:
    """What the brand tells the check about itself: the phone numbers, emails and website names it has written (so a post may carry
    them), the safety profile it chose, and the language it writes in. Read from the brand profile as plain text, so it works in any
    language. Kept for a minute; a brand that cannot be read has no contacts, the general profile and no language."""
    import json
    import time

    from app.agents.content_guard.sensitive import contact_keys
    from app.db.mongo import brand_profiles

    if not brand_id:
        return frozenset(), profiles.DEFAULT_PROFILE, None
    hit = _contact_cache.get(brand_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        doc = await brand_profiles.find_one({"id": brand_id}, {"_id": 0}) or {}
        result = (frozenset(contact_keys(json.dumps(doc, default=str, ensure_ascii=False))), profiles.known(doc.get("safety_profile")), doc.get("language") or None)
    except Exception as exc:  # noqa: BLE001 - without the brand's contacts a phone number is held, which is the safe side
        logger.warning("Could not read the brand's own contacts: %s", exc)
        result = (frozenset(), profiles.DEFAULT_PROFILE, None)
    _contact_cache[brand_id] = (time.monotonic() + _CONTACT_SECONDS, result)
    return result


async def brand_contact_keys(brand_id: Optional[str]) -> frozenset[str]:
    return (await brand_policy(brand_id))[0]


_language_now: ContextVar[str] = ContextVar("guard_language", default="")
_policy_note: ContextVar[str] = ContextVar("guard_policy_note", default="")


async def _read_with(judge: str, text: str) -> ScreenResult:
    """One judge's reading of the text. A long text is read in pieces so nothing past the first piece goes unread. A reply that says
    nothing (a provider that is down answers with an empty result) raises instead of counting as "safe"."""
    from app.prompts.registry import render_fragment
    from app.shared.llm import call_llm_structured

    allowed = ", ".join(c for c in CATEGORIES if c not in ("profanity", "assistant_leak", "custom"))
    for start in range(0, min(len(text), MAX_CHECK_CHARS * MAX_CHECK_PIECES), MAX_CHECK_CHARS):
        reply = await call_llm_structured(
            prompt=render_fragment("content_guard/screen", allowed=allowed, policy_note=_policy_note.get(), post=text[start:start + MAX_CHECK_CHARS]),
            system=render_fragment("content_guard/screen_system"),
            model=judges.JUDGES[judge],
            max_tokens=200,
        )
        if not isinstance(reply, dict) or "unsafe" not in reply:
            raise RuntimeError("the safety check returned no answer")
        if not reply.get("unsafe"):
            continue
        categories = [c for c in (reply.get("categories") or []) if c in CATEGORIES] or ["custom"]
        return ScreenResult(ok=False, categories=categories, matches=[str(reply.get("reason") or "")[:60]])
    return ScreenResult()


async def _shadow_read(shadow: str, primary: str, text: str, verdict: ScreenResult, language: str) -> None:
    """Read the same text with the silent judge and keep only the comparison. Nothing here can change what happens to a post."""
    try:
        other = await _read_with(shadow, text)
        judges.note_ok(shadow)
        await judges.record_comparison(primary=primary, primary_ok=verdict.ok, primary_categories=verdict.categories, shadow=shadow,
                                       shadow_ok=other.ok, shadow_categories=other.categories, text=text, language=language)
    except Exception as exc:  # noqa: BLE001
        judges.note_failure(shadow)
        await judges.record_comparison(primary=primary, primary_ok=verdict.ok, primary_categories=verdict.categories, shadow=shadow,
                                       shadow_ok=None, shadow_categories=[], text=text, language=language, error=str(exc)[:120])


async def _model_screen(text: str) -> ScreenResult:
    """A model's reading of the text, cached by content so a retried post is not checked twice. The read goes down the judges Ops
    chose; one that fails is noted and the next is tried, and only when every judge fails does the read raise (it is then never
    cached, so the post is read again next time, and the free rules stay the only screen meanwhile)."""
    key = hashlib.sha256((_policy_note.get() + "\n" + text).encode("utf-8")).hexdigest()
    if key in _model_verdicts:
        _model_verdicts.move_to_end(key)
        return _model_verdicts[key]
    cfg = guard_config.current()
    answered, last_error = "", None
    verdict = ScreenResult()
    for judge in judges.chain(cfg):
        if not judges.usable(judge):
            continue
        try:
            verdict = await _read_with(judge, text)
            judges.note_ok(judge)
            answered = judge
            break
        except Exception as exc:  # noqa: BLE001
            judges.note_failure(judge)
            last_error = exc
            logger.warning("Content Guard judge %s could not read the text: %s", judge, exc)
    if not answered:
        raise RuntimeError("the safety check returned no answer") from last_error
    shadow = judges.should_shadow(cfg)
    if shadow and shadow != answered:
        judges.spawn(_shadow_read(shadow, answered, text, verdict, _language_now.get()))
    _model_verdicts[key] = verdict
    while len(_model_verdicts) > _MODEL_CACHE:
        _model_verdicts.popitem(last=False)
    return verdict


async def _rewrite(text: str, categories: list[str]) -> str:
    from app.prompts.registry import render_fragment
    from app.shared.llm import GroqModel, call_llm

    problems = ", ".join(CATEGORIES.get(c, c).lower() for c in categories)
    return await call_llm(
        prompt=render_fragment("content_guard/rewrite", problems=problems, text=text),
        system=render_fragment("content_guard/rewrite_system"),
        model=GroqModel.FAST,
        temperature=0.4,
        max_tokens=1500,
    )


async def review_text(
    text: str,
    *,
    where: str = "output",
    workspace_id: Optional[str] = None,
    piece_id: Optional[str] = None,
    platform: Optional[str] = None,
    allow_rewrite: bool = True,
    stage: str = "generation",
    brand_id: Optional[str] = None,
    own_contacts: Optional[Iterable[str]] = None,
    language: Optional[str] = None,
) -> GuardResult:
    """Check `text` and return the version that is safe to use, or a blocked result when none can be made."""
    if workspace_id:
        from app.shared.llm import set_usage_workspace

        set_usage_workspace(workspace_id)  # safety checks are free, but their model calls are still counted per workspace
    if not isinstance(text, str) or not text.strip():
        return GuardResult(text=text or "")
    cfg = await guard_config.ensure_fresh()
    cleaned = clean_text(text)
    result = GuardResult(text=cleaned, outcome="fixed" if cleaned != text else "clean")
    result.ai_phrases = ai_phrases_in(cleaned, cfg.get("ai_phrases") or ())
    if not cfg.get("enabled", True):
        return result

    contacts, profile, brand_language = await brand_policy(brand_id)
    if own_contacts is None:
        own_contacts = contacts
    language = language or brand_language
    _language_now.set(language_profile(cleaned, language).code)
    _policy_note.set(profiles.note(cfg, profile))
    screened = rule_screen(cleaned, cfg, own_contacts, profile, language)
    caught_by = "rules"
    if screened.ok and _wants_model(cleaned, cfg, stage, language, profile):
        try:
            screened = await _model_screen(cleaned)
            caught_by = "model"
        except Exception as exc:
            logger.warning("Content Guard model check failed, rules only: %s", exc)
    if screened.ok:
        return result
    if caught_by == "model":
        level = await ladder.level_for(screened.categories)
        await ladder.log_decision(categories=screened.categories, level=level, action={1: "watched", 2: "advised", 3: "fixed or held", 4: "held"}[level],
                                  language=_language_now.get(), platform=platform, text=cleaned, stage=stage)
        if level == 1:
            return result
        if level == 2:
            result.notes = [CATEGORIES.get(c, c).lower() for c in screened.categories]
            return result
        if level == 4:
            allow_rewrite = False
    context = dict(
        source=caught_by, model_reason=(screened.matches[0] if caught_by == "model" and screened.matches else None),
        stage=stage, strictness=cfg.get("strictness"), model_check=cfg.get("model_check"),
    )

    result.categories, result.matches = screened.categories, screened.matches
    if allow_rewrite and cfg.get("rewrite_flagged", True):
        try:
            rewritten = clean_text(await _rewrite(cleaned, screened.categories))
            again = rule_screen(rewritten, cfg, own_contacts, profile, language)
            if rewritten.strip() and again.ok and not (_wants_model(rewritten, cfg, stage, language, profile) and not (await _model_screen(rewritten)).ok):
                result.text, result.outcome = rewritten, "rewritten"
                await record_event(outcome="rewritten", categories=screened.categories, matches=screened.matches, text=text,
                                   where=where, workspace_id=workspace_id, piece_id=piece_id, platform=platform,
                                   rewritten_text=rewritten, **context)
                return result
        except Exception as exc:
            logger.warning("Content Guard rewrite failed: %s", exc)

    result.outcome, result.message = "blocked", screened.message()
    await record_event(outcome="blocked", categories=screened.categories, matches=screened.matches, text=text,
                       where=where, workspace_id=workspace_id, piece_id=piece_id, platform=platform, **context)
    return result


HOLD_TEMPLATE = "This post was held back because it may contain {names}. Edit the wording, or ask for a rewrite, and it can go out."


async def hold_message(categories: list[str], language: Optional[str]) -> str:
    """Why a post was held, in the language the member writes in. English stays English. A blend such as Tanglish is read in English,
    because that is the language the member reads the screen in. Any other language is translated once and remembered; if the
    translation cannot be made the English words are used, so the reason is never missing."""
    english_names = ", ".join(CATEGORIES.get(c, c).lower() for c in categories)
    english = HOLD_TEMPLATE.format(names=english_names)
    code = (language or "").strip().lower()
    base = code.split("-")[0].split("_")[0]
    if not base or base == "en" or "+" in code:
        return english
    try:
        from app.shared.localized_strings import get_localized_string

        names = ", ".join([
            (await get_localized_string(f"safety.category.{c}", base, CATEGORIES.get(c, c).lower())) for c in categories
        ])
        return await get_localized_string("safety.hold", base, HOLD_TEMPLATE, {"names": names})
    except Exception as exc:  # noqa: BLE001 - the reason must always be there
        logger.warning("Could not translate the hold reason into %s: %s", base, exc)
        return english


async def guard_piece_doc(doc: dict[str, Any], *, where: str) -> dict[str, Any]:
    """Run the guard over a piece about to be saved, in place. The post text and every section are reviewed; hooks and
    search text are cleaned. A text that cannot be made safe stays as written but the piece is flagged for review, and the
    publish gate refuses it until someone edits it."""
    from app.agents.content_guard.rules import clean_value

    contacts, profile, brand_language = await brand_policy(doc.get("brand_id"))
    own = sorted(contacts)
    doc["own_contacts"] = own
    doc["safety_policy"] = {"profile": profile}
    doc["language"] = doc.get("language") or brand_language
    ids = {"workspace_id": doc.get("workspace_id"), "piece_id": doc.get("piece_id"), "platform": doc.get("platform"),
           "brand_id": doc.get("brand_id"), "own_contacts": own, "language": doc.get("language")}
    blocked: list[str] = []
    found: list[str] = []
    advised: list[str] = []

    async def review(value: Any) -> Any:
        if not isinstance(value, str) or not value.strip():
            return value
        outcome = await review_text(value, where=where, **ids)
        for n in outcome.notes:
            if n not in advised:
                advised.append(n)
        if outcome.blocked:
            blocked.extend(c for c in outcome.categories if c not in blocked)
            found.extend(m for m in outcome.matches if m and m not in found)
            return outcome.text
        return outcome.text

    doc["content"] = await review(doc.get("content"))
    sections = doc.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if isinstance(section, dict) and isinstance(section.get("content"), str):
                section["content"] = await review(section["content"])
    for key in ("hooks", "seo"):
        if doc.get(key):
            doc[key] = clean_value(doc[key])
    if blocked:
        names = ", ".join(CATEGORIES.get(c, c).lower() for c in blocked)
        doc["quality_passed"] = False
        doc["flagged_for_review"] = True
        doc["quality_issues"] = [*(doc.get("quality_issues") or []), f"May contain {names}."]
        # What the member sees on the post: why it was held back, which words set it off, and what to do.
        doc["safety_reason"] = {
            "categories": [CATEGORIES.get(c, c) for c in blocked],
            "matches": found[:6],
            "message": await hold_message(blocked, doc.get("language")),
        }
    if advised and not blocked:
        names = ", ".join(advised)
        doc["safety_note"] = {"categories": advised, "message": f"This post may contain {names}. It was not held back, but read it once more before it goes out."}
    return doc


async def check_piece_before_send(piece: dict[str, Any], workspace_id: str) -> None:
    """The readings taken once just before a post is scheduled or sent, remembered on the post so a retry costs nothing:
    a model's reading of the text (by its content, so an edit is read again) and a reading of each attached picture,
    recording or video. Results are stored as `safety_check` and `media_safety`, which the publish gate reads. A check
    that cannot run leaves the free rules as the only screen and is logged."""
    if workspace_id:
        from app.shared.llm import set_usage_workspace

        set_usage_workspace(workspace_id)  # safety checks are free, but their model calls are still counted per workspace
    cfg = await guard_config.ensure_fresh()
    if not cfg.get("enabled", True):
        return
    await _check_text_before_send(piece, workspace_id, cfg)
    if cfg.get("media_check", True):
        await _check_media_before_send(piece, workspace_id)


async def _check_text_before_send(piece: dict[str, Any], workspace_id: str, cfg: dict[str, Any]) -> None:
    from datetime import datetime, timezone

    from app.db.mongo import content_pieces

    text = str(piece.get("content") or "")
    profile = (piece.get("safety_policy") or {}).get("profile")
    if not text.strip() or not _wants_model(text, cfg, "publish", piece.get("language"), profile):
        return
    digest = content_hash(text)
    stored = piece.get("safety_check")
    if isinstance(stored, dict) and stored.get("hash") == digest:
        return
    if not rule_screen(text, cfg, piece.get("own_contacts") or (), profile, piece.get("language")).ok:
        return  # the gate already refuses it
    _policy_note.set(profiles.note(cfg, profile))
    try:
        verdict = await _model_screen(text)
    except Exception as exc:
        logger.warning("Content Guard model check could not run, rules only: %s", exc)
        return
    noted: list[str] = []
    if not verdict.ok:
        level = await ladder.level_for(verdict.categories)
        await ladder.log_decision(categories=verdict.categories, level=level, action={1: "watched", 2: "advised", 3: "held", 4: "held"}[level],
                                  language=str(piece.get("language") or ""), platform=piece.get("platform"), text=text, stage="publish")
        if level <= 2:
            noted = list(verdict.categories)
            verdict = ScreenResult()
    record = {"hash": digest, "ok": verdict.ok, "categories": verdict.categories, "checked_at": datetime.now(timezone.utc)}
    if noted:
        record["noted"] = noted
    piece["safety_check"] = record
    await content_pieces.update_one(
        {"piece_id": piece.get("piece_id"), "workspace_id": workspace_id}, {"$set": {"safety_check": record}},
    )
    if not verdict.ok:
        await record_event(
            outcome="blocked", categories=verdict.categories, matches=verdict.matches, text=text, where="publish",
            workspace_id=workspace_id, piece_id=piece.get("piece_id"), platform=piece.get("platform"),
            source="model", model_reason=(verdict.matches[0] if verdict.matches else None), stage="publish",
            brand_id=piece.get("brand_id"), user_id=piece.get("created_by"),
        )


async def _check_media_before_send(piece: dict[str, Any], workspace_id: str) -> None:
    from datetime import datetime, timezone

    from app.agents.content_guard.media import media_problem
    from app.db.mongo import content_pieces

    items = [m for m in (piece.get("media") or []) if isinstance(m, dict)]
    known = dict(piece.get("media_safety") or {})
    changed = False
    for item in items:
        media_id = item.get("id")
        marker = item.get("url") or item.get("poster_url") or str(item.get("transcript_language") or "")
        if not media_id or (isinstance(known.get(media_id), dict) and known[media_id].get("source") == marker):
            continue
        record = await media_problem(item, workspace_id)
        if record is None:
            continue
        record.update({"source": marker, "checked_at": datetime.now(timezone.utc)})
        known[media_id] = record
        changed = True
        if not record["ok"]:
            await record_event(
                outcome="blocked", categories=record["categories"], matches=[], text=f"[{item.get('kind')}] {media_id}",
                where="publish", workspace_id=workspace_id, piece_id=piece.get("piece_id"), platform=piece.get("platform"),
                source="media", stage="publish", media_kind=item.get("kind"), media_id=media_id, brand_id=piece.get("brand_id"),
            )
    if changed:
        piece["media_safety"] = known
        await content_pieces.update_one(
            {"piece_id": piece.get("piece_id"), "workspace_id": workspace_id}, {"$set": {"media_safety": known}},
        )
