"""The monthly cost-per-credit report for Ops: what a month's AI and render work would cost at paid rates, against the credits
charged for it.

Model calls are priced from the token totals the AI call log keeps per hour, provider and model (`llm_rollups`). Pictures are
priced per call. Voice, transcription and renders are priced from the minutes the credit ledger charged, because the call log
does not keep minutes. Free-tier models are priced at their paid equivalents where one exists, so the numbers hold once free
quotas run out. The target is $0.0035 or less for each credit (section 9.6a of the plan).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.billing import costs as C
from app.billing.credits import ledger_col, period_of
from app.db.mongo import get_db

TARGET_USD_PER_CREDIT = 0.0035

# dollars per million tokens in, out
TOKEN_RATES: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-embedding-001": (0.15, 0.0),
    "meta/llama-3.3-70b-instruct": (0.59, 0.79),
    "open-mistral-nemo": (0.15, 0.15),
}
# a model with no rate of its own is priced like the default writer, so nothing is counted as free by mistake
FALLBACK_TOKEN_RATE = TOKEN_RATES["openai/gpt-oss-120b"]
# dollars per call
CALL_RATES: dict[str, float] = {
    "@cf/black-forest-labs/flux-1-schnell": 0.0006, "black-forest-labs/FLUX.1-schnell": 0.0006, "flux": 0.0006,
    "gemini-2.5-flash-image": 0.039,
}
# dollars per minute, priced from the ledger's minutes
MINUTE_RATES: dict[str, float] = {
    "narration": 0.027, "dialogue": 0.027, "transcription": 0.0043, "captions": 0.0043, "cleanup": 0.0, "audio_mix": 0.0,
    "render": 0.02,
}
# models that are not AI work to price (health pings, wrappers counted under their real model)
_SKIP = {"ping", "call_llm_structured(BALANCED)", "audio-isolation", "aura-2-thalia-en", "nova-3", "whisper-large-v3", "whisper-large-v3-turbo"}


def _month_bounds(period: str) -> tuple[datetime, datetime]:
    from app.billing.credits import resets_at

    year, month = (int(p) for p in period.split("-"))
    return datetime(year, month, 1, tzinfo=timezone.utc), resets_at(period)


async def monthly(period: Optional[str] = None) -> dict[str, Any]:
    period = period or period_of()
    start, end = _month_bounds(period)
    models: list[dict[str, Any]] = []
    ai_cost = 0.0
    async for row in get_db()["llm_rollups"].aggregate([
        {"$match": {"hour": {"$gte": start, "$lt": end}}},
        {"$group": {"_id": "$model", "provider": {"$first": "$provider"}, "calls": {"$sum": "$calls"},
                    "tokens_in": {"$sum": "$tokens_in"}, "tokens_out": {"$sum": "$tokens_out"}}},
    ]):
        model = row["_id"] or ""
        if model in _SKIP:
            continue
        calls = int(row.get("calls") or 0)
        tin, tout = int(row.get("tokens_in") or 0), int(row.get("tokens_out") or 0)
        if model in CALL_RATES:
            cost = calls * CALL_RATES[model]
        else:
            rate_in, rate_out = TOKEN_RATES.get(model, FALLBACK_TOKEN_RATE)
            cost = tin / 1e6 * rate_in + tout / 1e6 * rate_out
        ai_cost += cost
        models.append({"model": model, "provider": row.get("provider"), "calls": calls, "tokens_in": tin, "tokens_out": tout,
                       "usd": round(cost, 4)})

    table = await C.costs()
    actions: list[dict[str, Any]] = []
    credits_total = 0
    minute_cost = 0.0
    async for row in ledger_col().aggregate([
        {"$match": {"period": period, "kind": "charge"}},
        {"$group": {"_id": "$action", "credits": {"$sum": "$credits"}, "units": {"$sum": "$units"}, "count": {"$sum": 1}}},
    ]):
        action = row["_id"] or ""
        credits = int(row.get("credits") or 0)
        credits_total += credits
        usd = float(row.get("units") or 0) * MINUTE_RATES[action] if action in MINUTE_RATES else None
        if usd is not None:
            minute_cost += usd
        actions.append({
            "id": action, "label": table.get(action, {}).get("label", action), "count": int(row.get("count") or 0),
            "units": round(float(row.get("units") or 0), 2), "credits": credits,
            "usd": round(usd, 4) if usd is not None else None,
            "usd_per_credit": round(usd / credits, 5) if usd is not None and credits else None,
            "over_target": bool(usd is not None and credits and usd / credits > TARGET_USD_PER_CREDIT),
        })
    actions.sort(key=lambda a: -a["credits"])
    total = ai_cost + minute_cost
    per_credit = total / credits_total if credits_total else None
    models.sort(key=lambda m: -m["usd"])
    return {
        "period": period, "credits_charged": credits_total, "ai_usd": round(ai_cost, 4), "minutes_usd": round(minute_cost, 4),
        "total_usd": round(total, 4), "usd_per_credit": round(per_credit, 5) if per_credit is not None else None,
        "target_usd_per_credit": TARGET_USD_PER_CREDIT, "over_target": bool(per_credit and per_credit > TARGET_USD_PER_CREDIT),
        "models": models, "actions": actions,
        "note": "Priced at paid rates, so the figures hold once free quotas run out. The AI cost covers every model call this month, "
                "including free system work, so it is shared across the credits charged.",
    }
