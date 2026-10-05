"""Ops LLM health and issues (platform staff). Replaces reading in-memory counters: everything here comes from
the saved records in app.shared.llm_health. Issues are addressed by their number (ISSUE-0142 is 142).

Everyone who is platform staff can view, acknowledge, fix, ignore, add notes, ping and share. Only a master
admin can change limits and alert rules."""

import time
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.core.auth import require_platform_staff
from app.core.config import settings
from app.core.middleware import limiter
from app.shared.llm_health import alerts, catalogue, health, issues, keycheck, reports, service
from app.shared.llm_health.recorder import Attempt, recorder
from app.shared.llm_health.scrub import scrub_message

router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _link(number: int | None = None) -> str:
    base = (settings.FRONTEND_URL or "").rstrip("/")
    return f"{base}/ops/llm/issues/{number}" if number is not None else f"{base}/ops/llm"


def _clean(value: Any) -> Any:
    """Makes a database document safe to send: ids to text, dates to ISO strings."""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _issue_out(doc: dict[str, Any]) -> dict[str, Any]:
    out = _clean({k: v for k, v in doc.items() if k != "sample_event_ids"})
    out["id"] = out.pop("_id", None)
    out["label"] = issues.issue_number_label(int(doc.get("number", 0)))
    manual = doc.get("source") == "manual"  # a manual issue keeps the words its author gave
    out["title"] = doc.get("title") if manual else issues.title_for(doc)
    out["summary"] = doc.get("summary", "") if manual else issues.summary_for(doc)
    kind = catalogue.kind_for(doc.get("error_type", "unknown"))
    features = doc.get("features") or ["this feature"]
    ctx = dict(provider=doc.get("provider") or "The provider", model=doc.get("model") or "the model", feature=features[0], prompt_path=(doc.get("prompt_paths") or [None])[0])
    out["what_to_do"] = {
        "staff": catalogue.render(kind.action_staff, **ctx), "dev": catalogue.render(kind.action_dev, **ctx),
        "long_term": catalogue.render(catalogue.LONG_TERM.get(doc.get("error_type", ""), ""), **ctx),
    }
    if doc.get("source") != "manual":
        out["auto_fix_after_h"] = issues.AUTO_FIX_AFTER_H.get(doc.get("error_type", ""), issues.DEFAULT_AUTO_FIX_H)
    if doc.get("error_type") in ("quota_daily", "quota_tokens") and doc.get("provider"):
        out["reset_at"] = health.next_reset(_now(), health.DEFAULT_RESET.get(doc["provider"], {"tz": "UTC", "hour": 0})).isoformat()
    out["affected_workspace_count"] = len(doc.get("affected_workspaces") or [])
    out.pop("affected_workspaces", None)
    return out


async def _with_fallback_outcome(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Whether the fallback has been covering for an issue: a fallback that worked for one of its features in
    the last 15 minutes means "working". Read from real events, never guessed."""
    from datetime import timedelta

    from app.db.mongo import llm_events

    since = _now() - timedelta(minutes=15)
    rows = await llm_events.find({"outcome": "fallback_success", "at": {"$gte": since}}, {"feature": 1}).to_list(500)
    covered = {r.get("feature") for r in rows}
    for d in docs:
        if d.get("source") == "manual":
            continue
        d["fallback_outcome"] = "working" if covered & set(d.get("features") or []) else d.get("fallback_outcome", "none")
        if d.get("error_type") == "fallback_failed":
            d["fallback_outcome"] = "none"
    return docs


async def _get_issue(number: int) -> dict[str, Any]:
    from app.db.mongo import llm_issues

    doc = await llm_issues.find_one({"number": number})
    if not doc:
        raise HTTPException(status_code=404, detail="Issue not found.")
    return doc


def _require_master(user: dict) -> None:
    if not user.get("is_master_admin"):
        raise HTTPException(status_code=403, detail="Only an admin can change this.")


# ---- the dashboard ---------------------------------------------------------------------------------------
@router.get("/health")
@limiter.limit("60/minute")
async def get_health(request: Request, range: Literal["1h", "24h", "7d"] = "24h", user: dict = Depends(require_platform_staff)) -> dict:
    from app.pipelines.media.image_generation import image_tiers_in_use

    tiers = image_tiers_in_use()
    return {
        **await service.overview(range),
        # Which picture services are switched on. With none, every picture is a plain text card.
        "picture_services": {"in_use": tiers, "none_on": not tiers},
    }


@router.get("/providers")
@limiter.limit("60/minute")
async def get_providers(request: Request, user: dict = Depends(require_platform_staff)) -> dict:
    return {"providers": await service.provider_states(), "config": await service.get_config()}


@router.post("/providers/ping")
@limiter.limit("6/minute")
async def ping(request: Request, user: dict = Depends(require_platform_staff)) -> dict:
    """Groq and Gemini get one tiny real call; the other providers only have their key checked, which uses none of their allowance. Rate limited and only runs on a click."""
    from app.shared.llm import llm_health_check

    t0 = time.perf_counter()
    result = await llm_health_check()
    result.update(await keycheck.check_keys())
    for provider in ("groq", "gemini", "cloudflare", "elevenlabs", "deepgram", "mistral", "nvidia", "openrouter", "huggingface"):
        r = result.get(provider) or {}
        if r.get("status") == "not_set":
            continue  # nothing was tried, so nothing is recorded
        ok = r.get("status") == "ok"
        recorder.record(Attempt(
            provider=provider, model="ping", ok=ok, latency_ms=float(r.get("latency_ms") or (time.perf_counter() - t0) * 1000),
            error_class=None if ok else "PingFailed", http_status=r.get("http_status"),
            error_message=None if ok else scrub_message(r.get("detail")), feature="provider_ping", count_in_rollup=provider in ("groq", "gemini"),
        ))
    return result


@router.get("/notifications")
@limiter.limit("60/minute")
async def notifications(request: Request, user: dict = Depends(require_platform_staff)) -> dict:
    """What the Ops bell shows for the LLM area: open serious issues and providers that are down or out of allowance.
    Worked out from the current state, so nothing is missed; the front end remembers what a person dismissed."""
    from app.db.mongo import llm_issues

    out: list[dict[str, Any]] = []
    docs = await llm_issues.find({"status": {"$in": ["open", "acknowledged"]}, "priority": {"$in": ["critical", "high"]}}).sort([("last_seen", -1)]).to_list(20)
    for d in docs:
        seen = d.get("last_seen")
        stamp = seen.isoformat() if hasattr(seen, "isoformat") else str(seen or "")
        manual = d.get("source") == "manual"
        out.append({
            "id": f"llm-issue-{d.get('number')}-{stamp}", "kind": "llm_issue", "at": stamp,
            "title": (d.get("title") if manual else issues.title_for(d)) or "AI problem",
            "detail": f"{issues.issue_number_label(int(d.get('number', 0)))}, {d.get('priority')} priority",
            "href": f"/ops/llm/issues/{d.get('number')}",
        })
    states = await service.provider_states()
    now = _now().isoformat()
    for name, info in states.items():
        if info["status"] in ("down", "limit_reached"):
            out.append({
                "id": f"llm-provider-{name}-{info['status']}-{now[:13]}", "kind": "llm_provider", "at": now,
                "title": f"{info.get('label', name)}: {info['status_text'].lower()}", "detail": info.get("used_for", ""), "href": "/ops/llm",
            })
    return {"items": out}


@router.get("/status-summary")
@limiter.limit("30/minute")
async def status_summary(request: Request, user: dict = Depends(require_platform_staff)) -> dict:
    from app.db.mongo import llm_issues

    data = await service.overview("24h")
    open_docs = await llm_issues.find({"status": {"$in": ["open", "acknowledged"]}}).to_list(50)
    open_docs.sort(key=lambda i: issues.PRIORITIES.index(i.get("priority", "medium")))
    text = reports.status_summary(data["banner"], {k: v["status"] for k, v in data["providers"].items()}, open_docs, link=_link())
    return {"text": text, "banner": data["banner"]}


# ---- issues -------------------------------------------------------------------------------------------------
@router.get("/issues")
@limiter.limit("60/minute")
async def list_issues(
    request: Request,
    status: Optional[str] = None, priority: Optional[str] = None, provider: Optional[str] = None,
    feature: Optional[str] = None, type: Optional[str] = None, owner: Optional[str] = None, q: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200), skip: int = Query(0, ge=0),
    user: dict = Depends(require_platform_staff),
) -> dict:
    from app.db.mongo import llm_issues

    query: dict[str, Any] = {}
    if status in issues.STATUSES:
        query["status"] = status
    elif status == "active":
        query["status"] = {"$in": ["open", "acknowledged"]}
    if priority in issues.PRIORITIES:
        query["priority"] = priority
    if provider:
        query["provider"] = provider
    if feature:
        query["features"] = feature
    if type:
        query["error_type"] = type
    if owner:
        query["owner_id"] = owner
    docs = await llm_issues.find(query).sort([("last_seen", -1)]).to_list(1000)
    if q:
        needle = q.lower()
        docs = [d for d in docs if needle in (d.get("title", "") if d.get("source") == "manual" else issues.title_for(d)).lower() or needle in issues.issue_number_label(int(d.get("number", 0))).lower() or needle in " ".join(d.get("prompt_paths") or []).lower()]
    docs.sort(key=lambda d: issues.PRIORITIES.index(d.get("priority", "medium")))
    total = len(docs)
    page = await _with_fallback_outcome(docs[skip:skip + limit])
    return {"items": [_issue_out(d) for d in page], "total": total}


class ManualIssue(BaseModel):
    title: str = Field(min_length=3, max_length=140)
    description: str = Field(default="", max_length=2000)
    feature: Optional[str] = Field(default=None, max_length=80)
    priority: Literal["critical", "high", "medium", "low"] = "medium"


@router.post("/issues")
@limiter.limit("20/minute")
async def create_manual_issue(request: Request, body: ManualIssue, user: dict = Depends(require_platform_staff)) -> dict:
    from app.db.mongo import llm_issues

    doc = issues.manual_issue_doc(
        number=await issues._next_number(), title=body.title, detail=body.description, priority=body.priority,
        feature=body.feature, by=user.get("id"), now=_now(),
    )
    await llm_issues.insert_one(doc)
    return _issue_out(doc)


@router.get("/issues/{number}")
@limiter.limit("60/minute")
async def get_issue(request: Request, number: int, user: dict = Depends(require_platform_staff)) -> dict:
    doc = (await _with_fallback_outcome([await _get_issue(number)]))[0]
    return _issue_out(doc)


@router.get("/issues/{number}/events")
@limiter.limit("60/minute")
async def issue_events(request: Request, number: int, limit: int = Query(20, ge=1, le=100), user: dict = Depends(require_platform_staff)) -> dict:
    from app.db.mongo import llm_events

    doc = await _get_issue(number)
    rows = await llm_events.find({"issue_id": doc["_id"]}).sort([("at", -1)]).to_list(limit)
    total = await llm_events.count_documents({"issue_id": doc["_id"]})
    return {"items": [{**_clean(r), "id": str(r["_id"])} for r in rows], "total": total}


class IssuePatch(BaseModel):
    status: Optional[Literal["open", "acknowledged", "fixed", "ignored"]] = None
    owner_id: Optional[str] = None
    priority: Optional[Literal["critical", "high", "medium", "low"]] = None
    reason: Optional[str] = Field(default=None, max_length=500)


@router.patch("/issues/{number}")
@limiter.limit("30/minute")
async def patch_issue(request: Request, number: int, body: IssuePatch, user: dict = Depends(require_platform_staff)) -> dict:
    from app.db.mongo import llm_issues

    doc = await _get_issue(number)
    now, who = _now(), user.get("id")
    sets: dict[str, Any] = {"updated_at": now}
    history: list[dict[str, Any]] = []
    if body.status and body.status != doc.get("status"):
        if body.status == "ignored" and not (body.reason or "").strip():
            raise HTTPException(status_code=422, detail="Say why you are ignoring this.")
        sets["status"] = body.status
        if body.status == "fixed":
            sets["resolution"] = {"by": who, "at": now, "note": scrub_message(body.reason or "", 500)}
        elif body.status in ("open", "acknowledged"):
            sets["resolution"] = None
        history.append({"at": now, "by": who, "change": f"status: {body.status}" + (f" ({scrub_message(body.reason, 200)})" if body.reason else "")})
    if body.owner_id is not None:
        sets["owner_id"] = body.owner_id or None
        history.append({"at": now, "by": who, "change": "owner changed"})
    if body.priority and body.priority != doc.get("priority"):
        if not (body.reason or "").strip():
            raise HTTPException(status_code=422, detail="Say why you are changing the priority.")
        sets["priority"], sets["priority_overridden"] = body.priority, True
        history.append({"at": now, "by": who, "change": f"priority: {body.priority} ({scrub_message(body.reason, 200)})"})
    update: dict[str, Any] = {"$set": sets}
    if history:
        update["$push"] = {"history": {"$each": history}}
    await llm_issues.update_one({"_id": doc["_id"]}, update)
    return _issue_out(await _get_issue(number))


class Note(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


@router.post("/issues/{number}/notes")
@limiter.limit("30/minute")
async def add_note(request: Request, number: int, body: Note, user: dict = Depends(require_platform_staff)) -> dict:
    from app.db.mongo import llm_issues

    doc = await _get_issue(number)
    await llm_issues.update_one({"_id": doc["_id"]}, {"$push": {"notes": {"at": _now(), "by": user.get("id"), "text": scrub_message(body.text, 1000)}}})
    return _issue_out(await _get_issue(number))


@router.get("/issues/{number}/report")
@limiter.limit("30/minute")
async def issue_report(request: Request, number: int, format: Literal["text", "markdown", "json"] = "text", user: dict = Depends(require_platform_staff)) -> dict:
    doc = await _get_issue(number)
    reset_at = None
    if doc.get("error_type") in ("quota_daily", "quota_tokens") and doc.get("provider"):
        reset_at = health.next_reset(_now(), health.DEFAULT_RESET.get(doc["provider"], {"tz": "UTC", "hour": 0}))
    text = reports.issue_report(doc, link=_link(number), reset_at=reset_at)
    if format == "json":
        return {"format": "json", "issue": _issue_out(doc), "text": text}
    return {"format": format, "text": text if format == "text" else "```\n" + text + "\n```"}


class Share(BaseModel):
    channel: Literal["slack", "email"] = "slack"


@router.post("/issues/{number}/share")
@limiter.limit("10/minute")
async def share_issue(request: Request, number: int, body: Share, user: dict = Depends(require_platform_staff)) -> dict:
    """Sends the report to the destinations an admin set up. The destination is never typed here."""
    doc = await _get_issue(number)
    text = reports.issue_report(doc, link=_link(number))
    rules = await service.get_alert_rules()
    if body.channel == "slack":
        ok = await alerts._send_slack(text) if settings.SLACK_WEBHOOK_URL else False
        if not settings.SLACK_WEBHOOK_URL:
            raise HTTPException(status_code=409, detail="Slack is not set up. Ask an admin to add the Slack address.")
    else:
        if not rules["email_to"]:
            raise HTTPException(status_code=409, detail="No email address is set for alerts. Ask an admin to add one.")
        ok = await alerts._send_email(list(rules["email_to"]), f"{issues.issue_number_label(number)}: {issues.title_for(doc)}", text)
    if not ok:
        raise HTTPException(status_code=502, detail="It could not be sent. Try again, or copy the report.")
    await service.audit("issue.share", user, f"{issues.issue_number_label(number)} sent by {body.channel}")
    return {"sent": True, "channel": body.channel}


# ---- settings (master admin) ---------------------------------------------------------------------------------
@router.get("/config")
@limiter.limit("30/minute")
async def get_config(request: Request, user: dict = Depends(require_platform_staff)) -> dict:
    return await service.get_config()


class ModelLimits(BaseModel):
    provider: str = Field(min_length=2, max_length=40)
    model: str = Field(min_length=2, max_length=120)
    limits: dict[str, int] = Field(default_factory=dict)
    warn_pct: float = Field(default=health.WARN_PCT, ge=1, le=100)
    critical_pct: float = Field(default=health.CRITICAL_PCT, ge=1, le=100)
    reset: dict[str, Any] = Field(default_factory=lambda: {"tz": "UTC", "hour": 0})


@router.put("/config")
@limiter.limit("20/minute")
async def put_config(request: Request, body: ModelLimits, user: dict = Depends(require_platform_staff)) -> dict:
    from app.db.mongo import llm_provider_config

    _require_master(user)
    allowed = {"rpm", "rpd", "tpm", "tpd"}
    limits = {k: int(v) for k, v in body.limits.items() if k in allowed and int(v) > 0}
    if body.warn_pct >= body.critical_pct:
        raise HTTPException(status_code=422, detail="The warning level has to be below the critical level.")
    await service.audit("config.limits", user, f"{body.provider} {body.model}: {limits}, warn {body.warn_pct}, critical {body.critical_pct}")
    await llm_provider_config.update_one(
        {"provider": body.provider, "model": body.model},
        {"$set": {"limits": limits, "warn_pct": body.warn_pct, "critical_pct": body.critical_pct, "reset": body.reset, "updated_at": _now(), "updated_by": user.get("id")}},
        upsert=True,
    )
    return await service.get_config()


@router.get("/storage")
@limiter.limit("20/minute")
async def get_storage(request: Request, user: dict = Depends(require_platform_staff)) -> dict:
    return await service.storage()


@router.get("/audit")
@limiter.limit("30/minute")
async def get_audit(request: Request, limit: int = Query(30, ge=1, le=100), user: dict = Depends(require_platform_staff)) -> dict:
    from app.db.mongo import llm_audit

    rows = await llm_audit.find({}).sort("at", -1).to_list(limit)
    return {"items": [{**_clean({k: v for k, v in r.items() if k != "_id"}), "id": str(r["_id"])} for r in rows]}


@router.get("/alerts")
@limiter.limit("30/minute")
async def get_alerts(request: Request, user: dict = Depends(require_platform_staff)) -> dict:
    return {"rules": await service.get_alert_rules(), "slack_configured": bool(settings.SLACK_WEBHOOK_URL), "email_configured": bool(settings.RESEND_API_KEY)}


class AlertRules(BaseModel):
    email_to: list[str] = Field(default_factory=list, max_length=10)
    slack: bool = True
    cooldown_minutes: int = Field(default=60, ge=5, le=1440)
    new_issue_min_priority: Literal["critical", "high", "medium", "low"] = "high"
    quota_warning: bool = True
    quota_critical: bool = True
    reopened: bool = True
    provider_down: bool = True


@router.put("/alerts")
@limiter.limit("20/minute")
async def put_alerts(request: Request, body: AlertRules, user: dict = Depends(require_platform_staff)) -> dict:
    from app.db.mongo import llm_alert_rules

    _require_master(user)
    emails = [e.strip().lower() for e in body.email_to if e.strip()]
    if any("@" not in e or "." not in e.split("@")[-1] for e in emails):
        raise HTTPException(status_code=422, detail="One of the email addresses is not valid.")
    doc = {**body.model_dump(), "email_to": emails}
    await service.audit("config.alerts", user, f"{len(emails)} email address(es), cooldown {body.cooldown_minutes} min, min priority {body.new_issue_min_priority}")
    await llm_alert_rules.update_one({"_id": "default"}, {"$set": doc}, upsert=True)
    return {"rules": await service.get_alert_rules()}


@router.post("/alerts/test")
@limiter.limit("5/minute")
async def test_alert(request: Request, user: dict = Depends(require_platform_staff)) -> dict:
    _require_master(user)
    rules = await service.get_alert_rules()
    alert = alerts.Alert("test", "test", "low", "Test alert", "This is a test from the LLM health page. Nothing is wrong.")
    return {"sent": await alerts.deliver(alert, rules)}
