"""What each prompt costs to run: calls, tokens in and out, cached tokens, time and failures, from real use.

The recorder keeps one small row for each prompt and day (`llm_prompt_daily`, 90 days). This turns those rows into what the Usage view
shows: totals for a window and for the window before it (so a change is visible), a day by day series with empty days filled in, and
the prompts ranked by tokens. The aggregation is plain functions over rows, so it is tested without a database.

Days recorded before tokens were kept have calls but no token figures. `token_calls` counts the calls the token figures cover, so an
average is never pulled down by older days, and `tokens_since` says from which day tokens are known."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from app.db.mongo import get_db

#: The windows the view offers, in days. Anything else is brought to the nearest of these.
WINDOWS = (7, 30, 90)
#: Most prompts listed.
TOP_LIMIT = 60

SUMS = ("calls", "failures", "token_calls", "tokens_in", "tokens_out", "cached_tokens", "latency_sum_ms")


def clean_days(days: Optional[int]) -> int:
    if days is None:
        return 30
    return min(WINDOWS, key=lambda w: abs(w - int(days)))


def _day(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return None


def _empty() -> dict[str, int]:
    return {k: 0 for k in SUMS}


def add_row(into: dict[str, int], row: dict[str, Any]) -> None:
    for key in SUMS:
        into[key] += int(row.get(key) or 0)


def totals(sums: dict[str, int]) -> dict[str, Any]:
    """Headline figures from raw sums. Averages are over the calls that carry token figures; a window with none says None."""
    tokens = sums["tokens_in"] + sums["tokens_out"]
    measured = sums["token_calls"]
    return {
        "calls": sums["calls"], "failures": sums["failures"],
        "failure_rate": round(sums["failures"] / sums["calls"], 4) if sums["calls"] else None,
        "tokens_in": sums["tokens_in"], "tokens_out": sums["tokens_out"], "tokens": tokens,
        "cached_tokens": sums["cached_tokens"],
        "cached_share": round(sums["cached_tokens"] / sums["tokens_in"], 4) if sums["tokens_in"] else None,
        "avg_tokens": round(tokens / measured) if measured else None,
        "avg_tokens_in": round(sums["tokens_in"] / measured) if measured else None,
        "avg_tokens_out": round(sums["tokens_out"] / measured) if measured else None,
        "avg_latency_ms": round(sums["latency_sum_ms"] / measured) if measured else None,
        "measured_calls": measured,
    }


def change(now: Optional[float], before: Optional[float]) -> Optional[float]:
    """How much `now` moved against `before`, as a fraction (0.25 is a quarter more). None when there is nothing to compare."""
    if now is None or before is None or before == 0:
        return None
    return round((now - before) / before, 4)


def build(rows: Iterable[dict[str, Any]], *, days: int, today: date, path: Optional[str] = None) -> dict[str, Any]:
    """The whole Usage answer from the rows of the window and of the window before it.

    With `path` it is one prompt's answer (its totals, series and change); without, every prompt's, with the ranking."""
    start = today - timedelta(days=days - 1)
    before_start = start - timedelta(days=days)
    series_index = {start + timedelta(days=i): i for i in range(days)}
    now_sums, before_sums = _empty(), _empty()
    per_day = [_empty() for _ in range(days)]
    by_path: dict[str, dict[str, Any]] = {}
    tokens_since: Optional[date] = None
    for row in rows:
        row_path, day = str(row.get("prompt_path") or ""), _day(row.get("day"))
        if day is None or (path is not None and row_path != path):
            continue
        if row.get("token_calls") and (tokens_since is None or day < tokens_since):
            tokens_since = day
        if day >= start and day <= today:
            add_row(now_sums, row)
            add_row(per_day[series_index[day]], row)
            slot = by_path.setdefault(row_path, {"sums": _empty(), "before": _empty(), "spark": [0] * days})
            add_row(slot["sums"], row)
            slot["spark"][series_index[day]] += int(row.get("tokens_in") or 0) + int(row.get("tokens_out") or 0)
        elif before_start <= day < start:
            add_row(before_sums, row)
            by_path.setdefault(row_path, {"sums": _empty(), "before": _empty(), "spark": [0] * days})
            add_row(by_path[row_path]["before"], row)
    grand = totals(now_sums)
    out: dict[str, Any] = {
        "days": days, "from": start.isoformat(), "to": today.isoformat(),
        "tokens_since": tokens_since.isoformat() if tokens_since else None,
        "totals": grand, "previous": totals(before_sums),
        "change": {k: change(grand[k], totals(before_sums)[k]) for k in ("calls", "tokens", "avg_tokens", "failure_rate")},
        "daily": [
            {"day": (start + timedelta(days=i)).isoformat(), "calls": d["calls"], "failures": d["failures"], "tokens_in": d["tokens_in"], "tokens_out": d["tokens_out"]}
            for i, d in enumerate(per_day)
        ],
    }
    if path is None:
        ranked = []
        for p, slot in by_path.items():
            t = totals(slot["sums"])
            if not t["calls"]:
                continue
            ranked.append({
                "path": p, **{k: t[k] for k in ("calls", "failures", "failure_rate", "tokens_in", "tokens_out", "tokens", "avg_tokens", "avg_latency_ms")},
                "share": round(t["tokens"] / grand["tokens"], 4) if grand["tokens"] else None,
                "change_tokens": change(t["tokens"], totals(slot["before"])["tokens"]), "spark": slot["spark"],
            })
        ranked.sort(key=lambda r: (r["tokens"], r["calls"]), reverse=True)
        out["prompts_used"] = len(ranked)
        out["top"] = ranked[:TOP_LIMIT]
    return out


async def load(*, days: int, path: Optional[str] = None, today: Optional[date] = None) -> dict[str, Any]:
    """Reads the two windows from the database and builds the answer."""
    days = clean_days(days)
    today = today or datetime.now(timezone.utc).date()
    first = datetime.combine(today - timedelta(days=2 * days - 1), datetime.min.time(), tzinfo=timezone.utc)
    flt: dict[str, Any] = {"day": {"$gte": first}}
    if path is not None:
        flt["prompt_path"] = path
    rows = [r async for r in get_db()["llm_prompt_daily"].find(flt, {"_id": 0})]
    return build(rows, days=days, today=today, path=path)
