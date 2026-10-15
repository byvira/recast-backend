"""Odette's video rules fire on built cases and stay quiet otherwise. The flags are the same descriptors the other rules produce."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.agents.supervisor import video_rules
from app.agents.supervisor.rules import _flag
from app.db.mongo import content_pieces, get_db, workspace_connections


async def _flag_of(flag_type, severity, detail, metric):
    return await _flag(flag_type, severity, detail, metric)


async def _run(ws: str) -> dict[str, dict]:
    flags = await video_rules.evaluate(ws, datetime.now(timezone.utc), _flag_of)
    return {f["flag_type"]: f for f in flags}


async def _workspace(tier: str = "single") -> str:
    ws = f"ws-{uuid4()}"
    await get_db()["workspaces"].insert_one({"id": ws, "tier": tier})
    return ws


async def _job(ws: str, status: str, seconds: float = 30, **extra) -> None:
    await get_db()["video_jobs"].insert_one({"id": uuid4().hex, "workspace_id": ws, "status": status, "duration_s": seconds,
                                              "created_at": datetime.now(timezone.utc), **extra})


async def test_a_quiet_workspace_gets_no_video_flags():
    assert await _run(await _workspace()) == {}


async def test_three_failed_renders_in_a_day_are_flagged_but_two_are_not_and_previews_do_not_count():
    ws = await _workspace()
    for _ in range(2):
        await _job(ws, "failed")
    await _job(ws, "failed", kind="proxy")
    assert "video_renders_failing" not in await _run(ws)

    await _job(ws, "blocked")

    flag = (await _run(ws))["video_renders_failing"]
    assert flag["severity"] == "warning" and flag["detail"]["failures"] == 3
    assert "3 video renders failed" in flag["summary_persona"]


async def test_render_minutes_near_and_over_the_limit_are_flagged_with_the_right_severity():
    ws = await _workspace("single")
    await _job(ws, "done", seconds=100 * 60)
    assert (await _run(ws))["video_minutes_high"]["severity"] == "warning"
    await _job(ws, "done", seconds=30 * 60)
    flag = (await _run(ws))["video_minutes_high"]
    assert flag["severity"] == "critical" and "130 of your 120" in flag["summary_persona"]


async def test_an_automatic_recipe_that_stopped_on_a_check_is_flagged_with_its_reason():
    ws = await _workspace()
    await get_db()["video_recipes"].insert_one({
        "id": uuid4().hex, "workspace_id": ws, "name": "Weekly clips",
        "automation": {"enabled": True, "paused": True, "paused_reason": "the captions did not match your voice"},
    })

    flag = (await _run(ws))["video_automation_stopped"]

    assert "the captions did not match your voice" in flag["summary_persona"] and "Nothing was published" in flag["summary_persona"]


async def test_a_scheduled_video_whose_connection_runs_out_first_is_flagged_and_one_that_does_not_is_not():
    ws = await _workspace()
    now = datetime.now(timezone.utc)
    await workspace_connections.insert_one({"workspace_id": ws, "platform": "linkedin", "is_active": True, "expires_at": now + timedelta(hours=5)})
    await content_pieces.insert_one({
        "piece_id": uuid4().hex, "workspace_id": ws, "platform": "LinkedIn", "deleted": False, "publish_status": "queued",
        "publish_scheduled_at": now + timedelta(hours=20), "attachments": [{"asset_type": "project", "asset_id": "p"}],
    })
    await content_pieces.insert_one({
        "piece_id": uuid4().hex, "workspace_id": ws, "platform": "LinkedIn", "deleted": False, "publish_status": "queued",
        "publish_scheduled_at": now + timedelta(hours=2), "attachments": [{"asset_type": "project", "asset_id": "p"}],
    })
    await content_pieces.insert_one({
        "piece_id": uuid4().hex, "workspace_id": ws, "platform": "LinkedIn", "deleted": False, "publish_status": "queued",
        "publish_scheduled_at": now + timedelta(hours=20), "attachments": [{"asset_type": "image", "asset_id": "i"}],
    })

    flag = (await _run(ws))["video_connection_expiring"]

    assert flag["detail"]["count"] == 1 and "1 scheduled video post " in flag["summary_persona"]


async def test_a_scheduled_video_made_before_the_brand_voice_last_changed_is_flagged_but_a_newer_one_is_not():
    from app.db.mongo import workspace_events

    ws = await _workspace()
    now = datetime.now(timezone.utc)
    await workspace_events.insert_one({"event_id": uuid4().hex, "workspace_id": ws, "event_type": "brand.voice_updated",
                                       "occurred_at": (now - timedelta(days=1)).isoformat(), "payload": {"brand_id": "b1"}})
    for made, brand in ((now - timedelta(days=3), "b1"), (now - timedelta(hours=2), "b1"), (now - timedelta(days=3), "other")):
        await content_pieces.insert_one({
            "piece_id": uuid4().hex, "workspace_id": ws, "brand_id": brand, "platform": "LinkedIn", "deleted": False, "publish_status": "queued",
            "created_at": made, "attachments": [{"asset_type": "project", "asset_id": "p"}],
        })

    flag = (await _run(ws))["video_voice_changed"]

    assert flag["detail"]["count"] == 1 and "Posts affected: 1." in flag["summary_persona"]
