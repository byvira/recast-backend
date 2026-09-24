"""Campaign cadence (app.agents.feedback.cadence) and the scheduler's backoff
for failing campaigns (app.workers.campaign_scheduler)."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.agents.feedback import cadence
from app.db.mongo import activity_entries, content_pieces, get_campaigns_collection, workspace_flags, workspace_insights
from app.workers import campaign_scheduler
from tests.conftest import create_workspace, signup_new_user


async def _campaign(ws_id, **overrides):
    now = datetime.now(timezone.utc)
    doc = {
        "id": str(uuid4()), "workspace_id": ws_id, "brand_id": "b", "name": "Launch",
        "topic_cluster": "t", "status": "active", "platforms": ["LinkedIn", "Instagram"],
        "cadence": {"frequency": "weekly", "days_per_batch": 3, "next_run_at": now + timedelta(days=3)},
        "piece_ids": [], "created_by": "u1", "deleted": False,
        "created_at": now, "updated_at": now,
    }
    doc.update(overrides)
    await get_campaigns_collection().insert_one(doc)
    return doc


def test_expected_posts_follow_the_configured_cadence():
    assert cadence.expected_posts_per_week({"cadence": {"frequency": "weekly", "days_per_batch": 3},
                                            "platforms": ["A", "B"]}) == 6
    assert cadence.expected_posts_per_week({"cadence": {"frequency": "daily", "days_per_batch": 1},
                                            "platforms": ["A"]}) == 7
    assert cadence.expected_posts_per_week({"cadence": {"frequency": "manual"}, "platforms": ["A"]}) == 0


async def test_failing_campaign_backs_off_instead_of_retrying_every_minute(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Backoff", tier="large")
    camp = await _campaign(ws_id)

    await campaign_scheduler._back_off(camp, "Brand profile is not complete.")
    stored = await get_campaigns_collection().find_one({"id": camp["id"]})
    assert stored["cadence"]["failures"] == 1
    next_run = stored["cadence"]["next_run_at"].replace(tzinfo=timezone.utc)
    assert timedelta(minutes=55) < next_run - datetime.now(timezone.utc) <= timedelta(hours=1)

    await campaign_scheduler._back_off(stored, "Brand profile is not complete.")
    stored = await get_campaigns_collection().find_one({"id": camp["id"]})
    next_run = stored["cadence"]["next_run_at"].replace(tzinfo=timezone.utc)
    assert next_run - datetime.now(timezone.utc) > timedelta(hours=1, minutes=55)   # 2h
    row = await activity_entries.find_one({"_id": f"system:campaign_run:{camp['id']}"})
    assert row["status"] == "failed" and row["metadata"]["retryAttempt"] == 2


async def test_stalled_campaign_flags_then_self_resolves(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Stalled", tier="large")
    now = datetime.now(timezone.utc)
    camp = await _campaign(ws_id, cadence={
        "frequency": "weekly", "days_per_batch": 3, "next_run_at": now - timedelta(days=8),
        "failures": 5, "last_error": "Brand profile is not complete.",
    })

    flag_id = await cadence.check_stalled(camp, now)
    assert flag_id
    assert await cadence.check_stalled(camp, now) is None          # one open flag per campaign
    row = await activity_entries.find_one({"_id": f"odette_flag:{flag_id}"})
    assert row["lane"] == "active" and row["title"] == "A campaign has stopped generating"

    camp["last_generated_at"] = now + timedelta(minutes=1)          # it ran again
    await cadence.check_stalled(camp, now + timedelta(minutes=2))
    assert (await workspace_flags.find_one({"_id": flag_id}))["status"] == "resolved"


async def test_behind_cadence_recommends_once(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Behind", tier="large")
    camp = await _campaign(ws_id)
    ids = []
    for i in range(6):   # expected 6/week; 1 published, 5 waiting
        pid = str(uuid4())
        ids.append(pid)
        await content_pieces.insert_one({
            "piece_id": pid, "workspace_id": ws_id, "campaign_id": camp["id"], "deleted": False,
            "approval_status": "pending",
            "publish_status": "published" if i == 0 else "pending",
            "published_at": datetime.now(timezone.utc) - timedelta(days=1),
        })
    camp["piece_ids"] = ids

    insight_id = await cadence.check_behind(camp, datetime.now(timezone.utc))
    insight = await workspace_insights.find_one({"_id": insight_id})
    assert insight["evidence"]["metrics"] == {
        "source": "cadence", "campaign_id": camp["id"],
        "published_7d": 1, "expected_per_week": 6, "waiting": 5,
    }
    assert await cadence.check_behind(camp, datetime.now(timezone.utc)) is None   # not repeated this week
