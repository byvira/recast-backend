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
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

from app.agents.content_guard import config as guard_config
from app.core.config import settings
from app.agents.content_guard.rules import CATEGORIES, ScreenResult, ai_phrases_in, clean_text, content_hash, risk_signals, screen_text
from app.agents.content_guard.store import record_event

logger = logging.getLogger(__name__)

OUTCOMES = ("clean", "fixed", "rewritten", "blocked")
MAX_CHECK_CHARS = 6000
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
        }


def _wants_model(text: str, cfg: dict[str, Any], stage: str) -> bool:
    """Whether a model should read this text: always for a post about to go out when Ops chose "publish", and for any text
    with risk signals (a listed bad word or a sensitive topic) when Ops chose "risky" or "publish"."""
    mode = cfg.get("model_check", "off")
    if mode == "off" or not settings.CONTENT_GUARD_LIVE_CHECKS:
        return False
    if mode == "publish" and stage == "publish":
        return True
    return bool(risk_signals(text))


def rule_screen(text: str, settings: Optional[dict[str, Any]] = None) -> ScreenResult:
    """The free rule screen with the current Ops settings. Synchronous, so the publish gate can use it."""
    cfg = settings or guard_config.current()
    if not cfg.get("enabled", True):
        return ScreenResult()
    return screen_text(
        text,
        strictness=cfg.get("strictness", "standard"),
        extra_blocked_terms=cfg.get("extra_blocked_terms", []),
        allowed_terms=cfg.get("allowed_terms", []),
    )


async def _model_screen(text: str) -> ScreenResult:
    """A small model's reading of the text, cached by content so a retried post is not checked twice."""
    from app.shared.llm import GroqModel, call_llm_structured

    key = hashlib.sha256(text[:MAX_CHECK_CHARS].encode("utf-8")).hexdigest()
    if key in _model_verdicts:
        _model_verdicts.move_to_end(key)
        return _model_verdicts[key]
    allowed = ", ".join(c for c in CATEGORIES if c not in ("profanity", "assistant_leak", "custom"))
    reply = await call_llm_structured(
        prompt=(
            "Read the post below and say whether it is unsafe to publish.\n"
            f"Unsafe means: {allowed}. Ordinary opinions, sales language and strong feelings are fine.\n"
            'Answer with JSON only: {"unsafe": true or false, "categories": [from the list], "reason": "a short phrase"}.\n\n'
            f"POST:\n{text[:MAX_CHECK_CHARS]}"
        ),
        system="You are a careful content safety reviewer. Answer with JSON only.",
        model=GroqModel.FAST,
        max_tokens=200,
    )
    categories = [c for c in (reply.get("categories") or []) if c in CATEGORIES]
    if reply.get("unsafe") and not categories:
        categories = ["custom"]
    verdict = ScreenResult(ok=not (reply.get("unsafe") and categories), categories=categories if reply.get("unsafe") else [],
                           matches=[str(reply.get("reason") or "")[:60]] if reply.get("unsafe") else [])
    _model_verdicts[key] = verdict
    while len(_model_verdicts) > _MODEL_CACHE:
        _model_verdicts.popitem(last=False)
    return verdict


async def _rewrite(text: str, categories: list[str]) -> str:
    from app.shared.llm import GroqModel, call_llm

    problems = ", ".join(CATEGORIES.get(c, c).lower() for c in categories)
    return await call_llm(
        prompt=(
            f"The post below may contain {problems}. Rewrite it so it keeps the same point, language, voice and roughly the same "
            "length but has none of that. Do not mention the rewrite. Do not use em dashes. Return only the post.\n\n"
            f"POST:\n{text}"
        ),
        system="You edit posts so they are safe to publish. Return only the edited post.",
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
) -> GuardResult:
    """Check `text` and return the version that is safe to use, or a blocked result when none can be made."""
    if not isinstance(text, str) or not text.strip():
        return GuardResult(text=text or "")
    cfg = await guard_config.ensure_fresh()
    cleaned = clean_text(text)
    result = GuardResult(text=cleaned, outcome="fixed" if cleaned != text else "clean")
    result.ai_phrases = ai_phrases_in(cleaned, cfg.get("ai_phrases") or ())
    if not cfg.get("enabled", True):
        return result

    screened = rule_screen(cleaned, cfg)
    caught_by = "rules"
    if screened.ok and _wants_model(cleaned, cfg, stage):
        try:
            screened = await _model_screen(cleaned)
            caught_by = "model"
        except Exception as exc:
            logger.warning("Content Guard model check failed, rules only: %s", exc)
    if screened.ok:
        return result
    context = dict(
        source=caught_by, model_reason=(screened.matches[0] if caught_by == "model" and screened.matches else None),
        stage=stage, strictness=cfg.get("strictness"), model_check=cfg.get("model_check"),
    )

    result.categories, result.matches = screened.categories, screened.matches
    if allow_rewrite and cfg.get("rewrite_flagged", True):
        try:
            rewritten = clean_text(await _rewrite(cleaned, screened.categories))
            again = rule_screen(rewritten, cfg)
            if rewritten.strip() and again.ok and not (_wants_model(rewritten, cfg, stage) and not (await _model_screen(rewritten)).ok):
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


async def guard_piece_doc(doc: dict[str, Any], *, where: str) -> dict[str, Any]:
    """Run the guard over a piece about to be saved, in place. The post text and every section are reviewed; hooks and
    search text are cleaned. A text that cannot be made safe stays as written but the piece is flagged for review, and the
    publish gate refuses it until someone edits it."""
    from app.agents.content_guard.rules import clean_value

    ids = {"workspace_id": doc.get("workspace_id"), "piece_id": doc.get("piece_id"), "platform": doc.get("platform")}
    blocked: list[str] = []
    found: list[str] = []

    async def review(value: Any) -> Any:
        if not isinstance(value, str) or not value.strip():
            return value
        outcome = await review_text(value, where=where, **ids)
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
            "message": f"This post was held back because it may contain {names}. Edit the wording, or ask for a rewrite, and it can go out.",
        }
    return doc


async def check_piece_before_send(piece: dict[str, Any], workspace_id: str) -> None:
    """The readings taken once just before a post is scheduled or sent, remembered on the post so a retry costs nothing:
    a model's reading of the text (by its content, so an edit is read again) and a reading of each attached picture,
    recording or video. Results are stored as `safety_check` and `media_safety`, which the publish gate reads. A check
    that cannot run leaves the free rules as the only screen and is logged."""
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
    if not text.strip() or not _wants_model(text, cfg, "publish"):
        return
    digest = content_hash(text)
    stored = piece.get("safety_check")
    if isinstance(stored, dict) and stored.get("hash") == digest:
        return
    if not rule_screen(text, cfg).ok:
        return  # the gate already refuses it
    try:
        verdict = await _model_screen(text)
    except Exception as exc:
        logger.warning("Content Guard model check could not run, rules only: %s", exc)
        return
    record = {"hash": digest, "ok": verdict.ok, "categories": verdict.categories, "checked_at": datetime.now(timezone.utc)}
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
