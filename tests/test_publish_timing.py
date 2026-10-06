"""Suggested publish times: the member's own results when there are enough, common times for the platform when there are not (and it
says so), no time in the past, a clash with a post already planned is shown, and times follow the member's time zone."""
from datetime import datetime, timedelta, timezone

import pytest

from app.db.mongo import content_pieces, post_metric_checkpoints
from app.pipelines.publish.timing import MAX_SLOTS, MIN_HISTORY, rank_slots
from tests.conftest import create_workspace
from tests.test_attachments import H, _piece

NOW = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)  # a Tuesday


def _sample(day_offset: int, hour_utc: int, engagement: float) -> dict:
    return {"published_at": (NOW - timedelta(days=day_offset)).replace(hour=hour_utc, minute=0), "engagement": engagement}


def _weekday_offsets(count: int) -> list[int]:
    """Days back from NOW that fall on a weekday (Monday to Friday)."""
    offsets = [d for d in range(1, 40) if (NOW - timedelta(days=d)).weekday() < 5]
    return offsets[:count]


def _history() -> list[dict]:
    """Weekday posts at 09:00 do best (9 posts), weekday 13:00 next (4), evenings worst (4)."""
    days = _weekday_offsets(17)
    rows = [_sample(d, 9, 0.08) for d in days[:9]]
    rows += [_sample(d, 13, 0.05) for d in days[9:13]]
    rows += [_sample(d, 19, 0.01) for d in days[13:17]]
    return rows


def _rank(samples, planned=(), tz="UTC", platform="linkedin"):
    return rank_slots(platform=platform, label="LinkedIn", samples=samples, planned=list(planned), tz_name=tz, now=NOW)


def test_with_enough_results_the_best_times_come_first_and_say_why():
    result = _rank(_history())

    assert result["basis"] == "history" and result["note"] is None
    first = result["slots"][0]
    assert first["confidence"] == "high"
    assert "weekday mornings" in first["reason"]
    assert datetime.fromisoformat(first["at"]).hour == 9
    assert result["slots"][1]["confidence"] in ("medium", "low")


def test_with_too_few_results_common_times_are_used_and_labelled_as_general():
    result = _rank(_history()[: MIN_HISTORY - 1])

    assert result["basis"] == "general"
    assert "don't have enough results" in result["note"]
    assert {s["confidence"] for s in result["slots"]} == {"general"}
    assert all("common high-traffic time" in s["reason"] for s in result["slots"])


def test_no_results_at_all_still_gives_slots():
    result = _rank([])
    assert result["basis"] == "general" and 1 <= len(result["slots"]) <= MAX_SLOTS


def test_no_suggestion_is_in_the_past_or_too_soon_and_none_repeat():
    for samples in (_history(), []):
        result = _rank(samples)
        times = [datetime.fromisoformat(s["at"]) for s in result["slots"]]
        assert all(t >= NOW + timedelta(minutes=30) for t in times)
        assert len(set(times)) == len(times)


def test_a_time_that_has_already_passed_today_moves_to_the_next_matching_day():
    # 09:00 UTC today is before NOW (10:00), so the best slot is the next weekday's 09:00.
    first = datetime.fromisoformat(_rank(_history())["slots"][0]["at"])
    assert first.date() == (NOW + timedelta(days=1)).date() and first.hour == 9


def test_times_follow_the_members_time_zone():
    # 09:00 in Kolkata (UTC+5:30) is 03:30 UTC. Results posted at 03:30 UTC are local 09:00, a weekday morning.
    rows = []
    for d in _weekday_offsets(10):
        rows.append({"published_at": (NOW - timedelta(days=d)).replace(hour=3, minute=30), "engagement": 0.09})
    result = _rank(rows, tz="Asia/Kolkata")

    first = datetime.fromisoformat(result["slots"][0]["at"])
    assert (first.hour, first.minute) == (3, 30)
    assert result["timezone"] == "Asia/Kolkata"


def test_an_unknown_time_zone_falls_back_to_utc():
    assert _rank(_history(), tz="Not/AZone")["timezone"] == "UTC"
    assert _rank(_history(), tz=None)["timezone"] == "UTC"


def test_a_post_already_planned_close_to_a_suggestion_is_shown_as_a_clash_and_ranked_after_clear_ones():
    clear = _rank(_history())
    best_at = datetime.fromisoformat(clear["slots"][0]["at"])

    result = _rank(_history(), planned=[best_at + timedelta(minutes=30)])

    clashing = [s for s in result["slots"] if s["clash"]]
    assert clashing and "already planned" in clashing[0]["clash"]
    flags = [s["clash"] is not None for s in result["slots"]]
    assert flags == sorted(flags)  # slots without a clash come first


def test_older_rows_with_text_dates_and_empty_numbers_do_not_break_it():
    rows = _history() + [{"published_at": "2026-09-30T09:00:00Z", "engagement": None}, {"published_at": None, "engagement": 0.2}, {"published_at": "junk", "engagement": 1}]
    result = _rank(rows, planned=["2026-10-07T09:00:00Z", None, "bad"])
    assert result["slots"]


# ── The route ─────────────────────────────────────────────────────────

async def test_the_route_reads_the_workspaces_results_and_planned_posts(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Timing WS 1")
    piece_id = await _piece(ws_id, profile["id"], platform="LinkedIn")
    now = datetime.now(timezone.utc)
    await post_metric_checkpoints.insert_many([
        {"_id": f"timing-{ws_id}-{i}", "workspace_id": ws_id, "piece_id": f"p{i}", "checkpoint": "24h", "platform": "linkedin",
         "published_at": now - timedelta(days=i + 1), "metrics": {"engagement_rate": 0.05}, "captured_at": now}
        for i in range(12)
    ])

    res = await client.get("/api/v1/publish/suggest-times", params={"piece_id": piece_id}, headers=H(ws_id))

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["basis"] == "history" and 1 <= len(body["slots"]) <= MAX_SLOTS and body["timezone"]


async def test_the_route_with_no_results_uses_general_patterns_and_a_missing_post_is_a_404(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Timing WS 2")
    piece_id = await _piece(ws_id, profile["id"], platform="Bluesky")

    general = await client.get("/api/v1/publish/suggest-times", params={"piece_id": piece_id}, headers=H(ws_id))
    missing = await client.get("/api/v1/publish/suggest-times", params={"piece_id": "nope"}, headers=H(ws_id))

    assert general.status_code == 200 and general.json()["basis"] == "general" and general.json()["note"]
    assert missing.status_code == 404


async def test_the_route_flags_a_clash_with_a_queued_post_on_the_same_platform(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Timing WS 3")
    piece_id = await _piece(ws_id, profile["id"], platform="LinkedIn")
    first = (await client.get("/api/v1/publish/suggest-times", params={"piece_id": piece_id}, headers=H(ws_id))).json()["slots"][0]["at"]
    other = await _piece(ws_id, profile["id"], platform="LinkedIn")
    await content_pieces.update_one(
        {"piece_id": other},
        {"$set": {"publish_status": "queued", "publish_target": "linkedin", "publish_scheduled_at": datetime.fromisoformat(first) + timedelta(minutes=20)}},
    )

    slots = (await client.get("/api/v1/publish/suggest-times", params={"piece_id": piece_id}, headers=H(ws_id))).json()["slots"]

    assert any(s["clash"] and "already planned" in s["clash"] for s in slots)
