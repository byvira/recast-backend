"""What each prompt costs to run: the recorder keeps tokens and time for each prompt and day, and the Usage routes turn that into totals, a
day by day series, a ranking and a change against the window before. Older days without token figures never pull an average down."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.db.mongo import get_db, users
from app.prompt_library import usage as U
from app.shared.llm_health.recorder import Attempt, Batch

TODAY = date(2026, 11, 4)
API = "/api/v1/ops/prompt-library"


def _row(path: str, days_ago: int, **fields) -> dict:
    day = datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time(), tzinfo=timezone.utc)
    return {"prompt_path": path, "day": day, "calls": fields.pop("calls", 0), **fields}


def test_the_recorder_keeps_what_each_call_cost_for_its_prompt_and_day():
    batch = Batch()
    for ok in (True, True, False):
        batch.add(Attempt(provider="groq", model="m", ok=ok, latency_ms=200.0, tokens_in=100, tokens_out=40, cached_tokens=10, prompt_path="text/refine/x"))
    (_, path), row = next(iter(batch.prompt_days.items()))
    assert path == "text/refine/x"
    assert row["calls"] == 3 and row["failures"] == 1 and row["token_calls"] == 3
    assert (row["tokens_in"], row["tokens_out"], row["cached_tokens"], row["latency_sum_ms"]) == (300, 120, 30, 600)


def test_the_windows_the_view_offers_are_seven_thirty_and_ninety_days():
    assert [U.clean_days(d) for d in (None, 1, 7, 20, 31, 60, 90, 400)] == [30, 7, 7, 30, 30, 30, 90, 90], "the nearest window, and the shorter one when it is a tie"


def test_totals_averages_and_change_come_from_the_window_and_the_one_before_it():
    rows = [
        _row("a/one", 1, calls=10, failures=1, token_calls=10, tokens_in=1000, tokens_out=500, cached_tokens=200, latency_sum_ms=5000),
        _row("a/one", 3, calls=10, failures=0, token_calls=10, tokens_in=1000, tokens_out=500, cached_tokens=0, latency_sum_ms=5000),
        _row("a/two", 2, calls=5, token_calls=5, tokens_in=250, tokens_out=50, latency_sum_ms=500),
        _row("a/one", 9, calls=10, token_calls=10, tokens_in=500, tokens_out=250),  # the window before
    ]
    out = U.build(rows, days=7, today=TODAY)
    t = out["totals"]
    assert t["calls"] == 25 and t["failures"] == 1 and t["tokens"] == 3300 and t["avg_tokens"] == 132
    assert t["failure_rate"] == 0.04 and t["cached_share"] == round(200 / 2250, 4)
    assert out["previous"]["tokens"] == 750 and out["change"]["tokens"] == round((3300 - 750) / 750, 4)
    assert [d["day"] for d in out["daily"]][0] == "2026-10-29" and out["daily"][-1]["day"] == "2026-11-04" and len(out["daily"]) == 7, "empty days are filled in"
    by_day = {d["day"]: d for d in out["daily"]}
    assert by_day["2026-11-03"]["tokens_in"] == 1000 and by_day["2026-11-01"]["tokens_in"] == 1000 and by_day["2026-11-02"]["tokens_in"] == 250
    assert by_day["2026-10-31"]["tokens_in"] == 0 and by_day["2026-10-31"]["calls"] == 0


def test_the_ranking_is_by_tokens_with_each_prompts_share_and_trend():
    rows = [
        _row("big/prompt", 1, calls=4, token_calls=4, tokens_in=6000, tokens_out=2000),
        _row("small/prompt", 1, calls=40, token_calls=40, tokens_in=1500, tokens_out=500),
        _row("big/prompt", 10, calls=4, token_calls=4, tokens_in=2000, tokens_out=0),
    ]
    out = U.build(rows, days=7, today=TODAY)
    assert [r["path"] for r in out["top"]] == ["big/prompt", "small/prompt"] and out["prompts_used"] == 2
    big = out["top"][0]
    assert big["tokens"] == 8000 and big["share"] == 0.8 and big["avg_tokens"] == 2000 and big["change_tokens"] == 3.0
    assert len(big["spark"]) == 7 and big["spark"][-2] == 8000


def test_days_recorded_before_tokens_were_kept_do_not_pull_an_average_down():
    rows = [
        _row("old/and/new", 2, calls=100),                                   # recorded before tokens were kept: calls only
        _row("old/and/new", 1, calls=10, token_calls=10, tokens_in=900, tokens_out=100),
    ]
    out = U.build(rows, days=7, today=TODAY)
    assert out["totals"]["calls"] == 110 and out["totals"]["avg_tokens"] == 100, "averaged over the 10 calls that carry tokens"
    assert out["tokens_since"] == "2026-11-03", "the view says from which day tokens are known"
    none = U.build([_row("only/old", 1, calls=5)], days=7, today=TODAY)
    assert none["totals"]["avg_tokens"] is None and none["tokens_since"] is None


def test_one_prompts_answer_leaves_every_other_prompt_out_and_has_no_ranking():
    rows = [_row("p/one", 1, calls=2, token_calls=2, tokens_in=10, tokens_out=10), _row("p/two", 1, calls=9, token_calls=9, tokens_in=90, tokens_out=90)]
    out = U.build(rows, days=7, today=TODAY, path="p/one")
    assert out["totals"]["calls"] == 2 and "top" not in out


async def _staff(signup_user):
    client, profile = await signup_user("Maker")
    await users.update_one({"id": profile["id"]}, {"$set": {"is_master_admin": True, "is_platform_staff": True}})
    return client


async def test_the_usage_routes_read_the_record_and_need_library_access(signup_user):
    db = get_db()
    now = datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    path = "text/refine/chips/shorten"
    await db["llm_prompt_daily"].delete_many({"prompt_path": path})
    await db["llm_prompt_daily"].insert_one({"_id": f"{day:%Y%m%d}|{path}", "day": day, "prompt_path": path, "calls": 6, "failures": 1, "token_calls": 6,
                                             "tokens_in": 600, "tokens_out": 300, "cached_tokens": 0, "latency_sum_ms": 1200})
    try:
        client = await _staff(signup_user)
        overview = await client.get(f"{API}/usage", params={"days": 30})
        assert overview.status_code == 200, overview.text
        body = overview.json()
        mine = next(r for r in body["top"] if r["path"] == path)
        assert body["days"] == 30 and mine["tokens"] == 900 and mine["calls"] == 6 and mine["klass"] in ("ops", "hybrid", "dev")
        one = await client.get(f"{API}/usage/prompt", params={"path": path, "days": 7})
        assert one.status_code == 200, one.text
        assert one.json()["totals"]["tokens"] == 900 and len(one.json()["daily"]) == 7 and one.json()["versions"] == []
        assert (await client.get(f"{API}/usage/prompt", params={"path": "no/such/prompt"})).status_code == 404
        stranger, _ = await signup_user("Customer")
        assert (await stranger.get(f"{API}/usage")).status_code in (401, 403)
    finally:
        await db["llm_prompt_daily"].delete_many({"prompt_path": path})
