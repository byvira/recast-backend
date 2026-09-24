"""Trust score, shadow mode (app.agents.feedback.trust). Nothing here may
ever publish — only score, log agreement, and (when earned) recommend."""

from datetime import datetime, timezone
from uuid import uuid4

from app.agents.feedback import trust
from app.db.mongo import activity_entries, autonomy_shadow, autonomy_trust, content_pieces, workspace_insights
from app.pipelines.text.storage import update_piece_status
from tests.conftest import create_workspace, signup_new_user


async def _pieces(ws_id, n, *, approval, version_count=1, publish="pending", quality=True, platform="LinkedIn"):
    ids = []
    for _ in range(n):
        pid = str(uuid4())
        ids.append(pid)
        await content_pieces.insert_one({
            "piece_id": pid, "workspace_id": ws_id, "session_id": "s", "platform": platform,
            "content": "x", "approval_status": approval, "version_count": version_count,
            "publish_status": publish, "quality_passed": quality, "deleted": False,
            "created_at": datetime.now(timezone.utc),
        })
    return ids


async def test_no_score_below_minimum_history(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Thin", tier="large")
    await _pieces(ws_id, 5, approval="approved")
    assert (await trust.compute_trust(ws_id, "LinkedIn"))["score"] is None


async def test_score_reflects_edits_rejections_quality_and_delivery(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Scored", tier="large")
    await _pieces(ws_id, 8, approval="approved", publish="published")               # untouched, live
    await _pieces(ws_id, 2, approval="approved", version_count=3, publish="failed")  # edited, failed
    await _pieces(ws_id, 2, approval="rejected", quality=False)
    t = await trust.compute_trust(ws_id, "LinkedIn")
    assert t["decided"] == 12
    assert t["components"] == {"untouched": 0.8, "kept": 0.833, "quality": 0.833, "delivered": 0.8}
    # 0.35*0.8 + 0.25*0.833 + 0.2*0.833 + 0.2*0.8 = 0.815
    assert t["score"] == 82


async def test_human_decisions_are_logged_against_the_score(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Shadow", tier="large")
    await _pieces(ws_id, 12, approval="approved", publish="published")   # score 100 → would auto-publish
    [edited] = await _pieces(ws_id, 1, approval="pending", version_count=2)
    [clean] = await _pieces(ws_id, 1, approval="pending")

    await update_piece_status(clean, ws_id, approval_status="approved")
    await update_piece_status(edited, ws_id, approval_status="approved")

    logs = {d["piece_id"]: d for d in await autonomy_shadow.find({"workspace_id": ws_id}).to_list(10)}
    assert logs[clean]["would_auto_publish"] and logs[clean]["agree"]
    assert logs[edited]["human_action"] == "approved_after_edit" and not logs[edited]["agree"]
    doc = await autonomy_trust.find_one({"workspace_id": ws_id})
    assert doc["shadow"] == {"total": 2, "agree": 1}
    # Shadow only: the pieces were approved, never published by the score.
    assert (await content_pieces.find_one({"piece_id": clean}))["publish_status"] == "pending"


async def test_strong_agreement_earns_one_admin_recommendation(api_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Earned", tier="large")
    await _pieces(ws_id, 12, approval="approved", publish="published")
    await trust.refresh_tuple(ws_id, "LinkedIn")
    await autonomy_trust.update_one({"workspace_id": ws_id}, {"$set": {"shadow": {"total": 25, "agree": 24}}})

    res = await trust.autonomy_trust_refresh()
    assert res["recommended"] >= 1
    insight = await workspace_insights.find_one({"workspace_id": ws_id, "evidence.metrics.source": "trust_shadow"})
    assert insight["title"] == "LinkedIn drafts look ready for auto-publish"
    row = await activity_entries.find_one({"_id": f"odette_insight:{insight['_id']}"})
    assert row["lane"] == "active" and row["visibility"] == "admins"

    await trust.autonomy_trust_refresh()   # no repeat within 30 days
    assert await workspace_insights.count_documents(
        {"workspace_id": ws_id, "evidence.metrics.source": "trust_shadow"}) == 1


async def test_autonomy_api_is_admin_read_owner_write(api_client, make_client):
    from tests.conftest import invite_and_accept
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Autonomy API", tier="large")
    editor, _ = await invite_and_accept(api_client, make_client, ws_id, "editor")
    h = {"X-Workspace-Id": ws_id}
    assert (await editor.get("/api/v1/activity/autonomy", headers=h)).status_code == 403
    res = await api_client.put("/api/v1/activity/autonomy/threshold",
                               json={"platform": "LinkedIn", "threshold": 90}, headers=h)
    assert res.status_code == 200, res.text
    assert res.json()["threshold"] == 90
    listing = (await api_client.get("/api/v1/activity/autonomy", headers=h)).json()
    assert listing["mode"] == "shadow"
    assert listing["items"][0]["threshold"] == 90
