"""Reads the saved records and turns them into what the dashboard shows: quota use, provider status, the
hourly chart, the feature table. The rules themselves are in health.py; this module only gathers numbers."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.shared.llm_health import alerts, health, issues
from app.shared.llm_health.recorder import recorder

logger = logging.getLogger(__name__)

RANGES = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _version() -> str | None:
    from app.shared.llm_health.recorder import app_version

    return app_version()


AUDIT_COLLECTIONS = ("llm_events", "llm_rollups", "llm_prompt_daily", "llm_issues", "llm_alert_log", "llm_audit")
STORAGE_BUDGET_MB = 100  # what the module is designed to stay under; the Atlas free plan is 512 MB for everything


async def storage() -> dict[str, Any]:
    """How much room the health records use. Size is read from the database when it allows it."""
    from app.db.mongo import get_db

    db = get_db()
    rows, total = [], 0
    for name in AUDIT_COLLECTIONS:
        count = await db[name].count_documents({})
        size = None
        try:
            stats = await db.command("collStats", name)
            size = int(stats.get("size", 0)) + int(stats.get("totalIndexSize", 0))
            total += size
        except Exception:  # noqa: BLE001 - a plan that does not allow collStats still shows counts
            pass
        rows.append({"collection": name, "documents": count, "bytes": size})
    return {"collections": rows, "total_bytes": total or None, "budget_mb": STORAGE_BUDGET_MB}


async def audit(action: str, user: dict, detail: str) -> None:
    """Who did what to the settings, an issue or a share. Never raises."""
    try:
        from app.db.mongo import llm_audit

        await llm_audit.insert_one({"at": _now(), "by": user.get("id"), "action": action, "detail": detail[:300]})
    except Exception as exc:  # noqa: BLE001
        logger.error("LLM audit write failed: %s", exc)


# ---- configuration -------------------------------------------------------------------------------------
async def get_config() -> dict[str, Any]:
    """Limits per provider and model: the saved ones, or the owner's starting figures. `source` says which."""
    from app.db.mongo import llm_provider_config

    saved = {(d["provider"], d["model"]): d async for d in llm_provider_config.find({})}
    rows = []
    seen = set()
    for provider, models in health.DEFAULT_LIMITS.items():
        for model, limits in models.items():
            doc = saved.get((provider, model))
            seen.add((provider, model))
            rows.append({"provider": provider, "model": model, "limits": (doc or {}).get("limits", limits),
                         "warn_pct": (doc or {}).get("warn_pct", health.WARN_PCT), "critical_pct": (doc or {}).get("critical_pct", health.CRITICAL_PCT),
                         "reset": (doc or {}).get("reset", health.DEFAULT_RESET[provider]), "source": "saved" if doc else health.DEFAULT_SOURCE.get(provider, health.OWNER_SOURCE)})
    for (provider, model), doc in saved.items():
        if (provider, model) not in seen:
            rows.append({"provider": provider, "model": model, "limits": doc.get("limits", {}), "warn_pct": doc.get("warn_pct", health.WARN_PCT),
                         "critical_pct": doc.get("critical_pct", health.CRITICAL_PCT), "reset": doc.get("reset", health.DEFAULT_RESET.get(provider, {"tz": "UTC", "hour": 0})), "source": "saved"})
    return {"models": rows, "reset_defaults": health.DEFAULT_RESET}


async def get_alert_rules() -> dict[str, Any]:
    from app.db.mongo import llm_alert_rules

    doc = await llm_alert_rules.find_one({"_id": "default"}) or {}
    doc.pop("_id", None)
    return {**alerts.DEFAULT_RULES, **doc}


# ---- usage ------------------------------------------------------------------------------------------------
async def usage_since(since: datetime, *, provider: str | None = None, model: str | None = None) -> dict[str, float]:
    from app.db.mongo import llm_rollups

    match: dict[str, Any] = {"hour": {"$gte": since.replace(minute=0, second=0, microsecond=0)}}
    if provider:
        match["provider"] = provider
    if model:
        match["model"] = model
    rows = await llm_rollups.aggregate([{"$match": match}, {"$group": {"_id": None, "calls": {"$sum": "$calls"}, "tokens_in": {"$sum": "$tokens_in"}, "tokens_out": {"$sum": "$tokens_out"}}}]).to_list(1)
    r = rows[0] if rows else {}
    return {"calls": r.get("calls", 0), "tokens": r.get("tokens_in", 0) + r.get("tokens_out", 0)}


async def models_today(now: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
    """Every model that was called since its provider's allowance last reset, with calls and tokens, whether or not a limit is set for it.
    A model with no limit still shows what it used, so the owner can see what to enter from the provider's console."""
    from app.db.mongo import llm_rollups

    now = now or _now()
    cfg = {(c["provider"], c["model"]): c for c in (await get_config())["models"]}
    reset = {p: health.last_reset(now, rule) for p, rule in health.DEFAULT_RESET.items()}
    earliest = min(reset.values(), default=now - timedelta(days=1))
    rows = await llm_rollups.aggregate([
        {"$match": {"hour": {"$gte": earliest.replace(minute=0, second=0, microsecond=0)}}},
        {"$group": {"_id": {"p": "$provider", "m": "$model", "h": "$hour"}, "calls": {"$sum": "$calls"}, "failures": {"$sum": "$failures"},
                    "tin": {"$sum": "$tokens_in"}, "tout": {"$sum": "$tokens_out"}}},
    ]).to_list(5000)
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        provider, model, hour = r["_id"]["p"], r["_id"]["m"], r["_id"]["h"]
        start = reset.get(provider) or health.last_reset(now, {"tz": "UTC", "hour": 0})
        if hour.replace(tzinfo=hour.tzinfo or timezone.utc) < start.replace(minute=0, second=0, microsecond=0):
            continue
        m = out.setdefault(provider, {}).setdefault(model, {"model": model, "calls": 0, "failures": 0, "tokens": 0})
        m["calls"] += int(r.get("calls", 0)); m["failures"] += int(r.get("failures", 0)); m["tokens"] += int(r.get("tin", 0)) + int(r.get("tout", 0))
    final: dict[str, list[dict[str, Any]]] = {}
    for provider, models in out.items():
        for model, m in models.items():
            limits = (cfg.get((provider, model)) or {}).get("limits") or {}
            m["limit_set"] = any(limits.get(k) for k in ("rpd", "tpd", "rpm", "tpm"))
        final[provider] = sorted(models.values(), key=lambda m: -m["calls"])
    return final


async def quota_snapshot(now: datetime | None = None) -> list[dict[str, Any]]:
    """One row per model per day window with a limit: how much is used, the level, when it resets, the projection."""
    now = now or _now()
    out = []
    for cfg in (await get_config())["models"]:
        rule = cfg["reset"]
        start = health.last_reset(now, rule)
        reset_at = health.next_reset(now, rule)
        used = await usage_since(start, provider=cfg["provider"], model=cfg["model"])
        used.update(minute_usage(cfg["provider"], cfg["model"]))
        for row in health.quota_rows(cfg["limits"], used, warn=cfg["warn_pct"], critical=cfg["critical_pct"]):
            minute = row["window"] in ("rpm", "tpm")
            proj = None if minute else health.projection(row["used"], row["limit"], start, now, reset_at)
            out.append({**row, "provider": cfg["provider"], "model": cfg["model"], "source": cfg["source"],
                        "window_start": start.strftime("%Y%m%d%H"),
                        "reset_at": (now + timedelta(seconds=60)).isoformat() if minute else reset_at.isoformat(),
                        "rolling": minute,
                        "runs_out_at": proj["runs_out_at"].isoformat() if proj and proj["before_reset"] else None})
    return out


def minute_usage(provider: str, model: str) -> dict[str, float]:
    """Calls and tokens in the last 60 seconds for one model. Counted in this server's memory, so it is only
    this server's share: with more than one server it is a lower bound, and it starts from zero after a restart."""
    cutoff = time.time() - 60
    rows = [r for r in recorder.recent.get(provider, []) if r[0] >= cutoff and r[4] == model]
    return {"calls_min": len(rows), "tokens_min": sum(r[5] for r in rows)}


def recent_provider_stats(minutes: int = health.STATUS_WINDOW_MIN) -> dict[str, dict[str, Any]]:
    cutoff = time.time() - minutes * 60
    stats: dict[str, dict[str, Any]] = {}
    for provider, ring in recorder.recent.items():
        rows = [r for r in ring if r[0] >= cutoff]
        stats[provider] = {"calls": len(rows), "successes": sum(1 for r in rows if r[1]), "fallback": any(r[2] for r in rows), "auth_failed": any(r[3] for r in rows)}
    return stats


async def provider_states(now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Status, quota rows and recent numbers for every known provider."""
    from app.db.mongo import llm_issues

    now = now or _now()
    quota = await quota_snapshot(now)
    today = await models_today(now)
    recent = recent_provider_stats()
    open_issues = await llm_issues.find({"status": {"$in": ["open", "acknowledged"]}}).to_list(200)
    names = set(health.PROVIDERS) | set(health.DEFAULT_LIMITS) | set(recent)
    out = {}
    for name in sorted(names):
        rows = [q for q in quota if q["provider"] == name]
        r = recent.get(name, {"calls": 0, "successes": 0, "fallback": False, "auth_failed": False})
        prios = [i.get("priority", "medium") for i in open_issues if i.get("provider") == name]
        state = health.provider_status(
            calls=r["calls"], successes=r["successes"], quota_levels=[q["level"] for q in rows], open_issue_priorities=prios,
            auth_failed=r["auth_failed"], limit_reached=any((q["percent"] or 0) >= 100 for q in rows), fallback_used=r["fallback"],
        )
        busiest = max(rows, key=lambda q: q["percent"] or 0, default=None)
        meta = health.PROVIDERS.get(name, {"label": name.capitalize(), "used_for": ""})
        out[name] = {"configured": health.is_configured(name), "label": meta["label"], "used_for": meta["used_for"], "status": state, "status_text": health.STATUS_TEXT[state],
                     "recent": r, "quota": rows, "busiest": busiest, "today": today.get(name, []),
                     **({"pool": health.pool_estimate(sum(m["calls"] - m["failures"] for m in today.get(name, []) if m["model"].startswith(health.CLOUDFLARE_POOL["model_prefix"])))} if name == "cloudflare" else {})}
    return out


# ---- the dashboard's numbers ---------------------------------------------------------------------------------
def _failure_rows(causes: dict[tuple[str, str], int], limit: int = 12) -> list[dict[str, Any]]:
    """Failures grouped by provider and plain cause, biggest first, each with the wording the issues use (no provider text, no codes)."""
    from app.shared.llm_health import catalogue

    out = []
    for (provider, kind), n in sorted(causes.items(), key=lambda kv: -kv[1])[:limit]:
        meta = health.PROVIDERS.get(provider, {"label": provider.capitalize()})
        out.append({"provider": provider, "label": meta["label"], "kind": kind, "cause": catalogue.render(catalogue.kind_for(kind).cause, provider=meta["label"]), "count": n})
    return out


async def overview(range_key: str = "24h") -> dict[str, Any]:
    from app.db.mongo import llm_issues, llm_rollups

    now = _now()
    span = RANGES.get(range_key, RANGES["24h"])
    since = (now - span).replace(minute=0, second=0, microsecond=0)
    rows = await llm_rollups.find({"hour": {"$gte": since}}).to_list(None)

    tiles = {"calls": 0, "successes": 0, "failures": 0, "rate_limited": 0, "cache_tokens": 0, "tokens_in": 0, "latency_sum": 0, "latency_count": 0}
    series: dict[datetime, dict[str, int]] = {}
    features: dict[str, dict[str, int]] = {}
    causes: dict[tuple[str, str], int] = {}
    for r in rows:
        tiles["calls"] += r.get("calls", 0)
        tiles["successes"] += r.get("successes", 0)
        tiles["failures"] += r.get("failures", 0)
        tiles["cache_tokens"] += r.get("cached_tokens", 0)
        tiles["tokens_in"] += r.get("tokens_in", 0)
        tiles["latency_sum"] += r.get("latency_sum_ms", 0)
        tiles["latency_count"] += r.get("latency_count", 0)
        errs = r.get("errors", {})
        for kind, n in errs.items():
            if n:
                causes[(r.get("provider") or "unknown", kind)] = causes.get((r.get("provider") or "unknown", kind), 0) + n
        tiles["rate_limited"] += errs.get("rate_limit_minute", 0) + errs.get("quota_daily", 0) + errs.get("quota_tokens", 0)
        hour = series.setdefault(r["hour"], {"successes": 0, "failures": 0})
        hour["successes"] += r.get("successes", 0)
        hour["failures"] += r.get("failures", 0)
        f = features.setdefault(r.get("feature", "unknown"), {"calls": 0, "failures": 0, "latency_sum": 0, "latency_count": 0})
        f["calls"] += r.get("calls", 0)
        f["failures"] += r.get("failures", 0)
        f["latency_sum"] += r.get("latency_sum_ms", 0)
        f["latency_count"] += r.get("latency_count", 0)

    providers = await provider_states(now)
    top = await llm_issues.find({"status": {"$in": ["open", "acknowledged"]}}).sort([("last_seen", -1)]).to_list(50)
    top.sort(key=lambda i: issues.PRIORITIES.index(i.get("priority", "medium")))
    top_title = issues.title_for(top[0]) if top else None
    states = {k: v["status"] for k, v in providers.items()}
    banner = health.overall(states, user_facing_failures=any(i.get("error_type") == "fallback_failed" for i in top), top_issue_title=top_title)

    return {
        "range": range_key, "banner": banner, "providers": providers,
        "tiles": {
            "calls": tiles["calls"],
            "succeeded_pct": round(tiles["successes"] / tiles["calls"] * 100, 1) if tiles["calls"] else None,
            "rate_limited": tiles["rate_limited"],
            "typical_wait_ms": round(tiles["latency_sum"] / tiles["latency_count"]) if tiles["latency_count"] else None,
            "cached_pct": round(tiles["cache_tokens"] / tiles["tokens_in"] * 100, 1) if tiles["tokens_in"] else None,
        },
        "series": [{"hour": h.isoformat(), **v} for h, v in sorted(series.items())],
        "by_feature": sorted(
            [{"feature": k, "calls": v["calls"], "failed": v["failures"], "typical_wait_ms": round(v["latency_sum"] / v["latency_count"]) if v["latency_count"] else None} for k, v in features.items()],
            key=lambda x: -x["calls"]),
        "by_failure": _failure_rows(causes),
        "recorder": recorder.status(),
        "retention": {"events_days": 30, "hourly_days": 90},
        "version": _version(),
        "generated_at": now.isoformat(),
    }


async def send_alerts(*, created: list[dict[str, Any]], reopened: list[dict[str, Any]]) -> int:
    """Decides and sends alerts for new and reopened issues and for quota and provider changes. Never raises."""
    try:
        from app.core.config import settings

        rules = await get_alert_rules()
        quota = [q for q in await quota_snapshot() if q["level"] in ("warning", "critical") and q["window"] in ("rpd", "tpd")]
        states = {k: v["status"] for k, v in (await provider_states()).items()}
        base = (settings.FRONTEND_URL or "").rstrip("/")
        candidate = alerts.decide(rules=rules, created=created, reopened=reopened, quota=quota, providers=states, recently_sent={}, now=_now(), base_url=base)
        if not candidate:
            return 0
        sent = await alerts.recently_sent([a.key for a in candidate], timedelta(minutes=int(rules["cooldown_minutes"])))
        final = alerts.decide(rules=rules, created=created, reopened=reopened, quota=quota, providers=states, recently_sent=sent, now=_now(), base_url=base)
        for alert in final:
            await alerts.deliver(alert, rules)
        return len(final)
    except Exception as exc:  # noqa: BLE001 - alerts must never affect the recorder
        logger.error("LLM alerting failed: %s", exc)
        return 0


async def send_daily_digest() -> bool:
    """The morning email about what is still open, to the addresses set in Alerts. Sent once a day and only when there is something to
    say, so a quiet day sends nothing. Never raises."""
    try:
        from app.core.config import settings
        from app.db.mongo import llm_alert_log, llm_events, llm_issues

        rules = await get_alert_rules()
        if not rules.get("daily_digest") or not rules.get("email_to"):
            return False
        now = _now()
        key = f"digest:{now.date().isoformat()}"
        if await alerts.recently_sent([key], timedelta(hours=20)):
            return False
        open_issues = await llm_issues.find({"status": {"$in": ["open", "acknowledged"]}}).to_list(200)
        states = {k: v["status"] for k, v in (await provider_states()).items()}
        failed = await llm_events.count_documents({"at": {"$gte": now - timedelta(hours=24)}, "outcome": "failed"})
        body = alerts.digest_body(open_issues, states, failed, (settings.FRONTEND_URL or "").rstrip("/"))
        if not body:
            return False
        sent = await alerts._send_email(list(rules["email_to"]), "AI health this morning", body)
        await llm_alert_log.insert_one({"at": now, "key": key, "kind": "digest", "subject": "AI health this morning", "result": {"email": sent}})
        return sent
    except Exception as exc:  # noqa: BLE001 - the digest must never affect anything else
        logger.error("LLM daily digest failed: %s", exc)
        return False
