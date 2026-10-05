"""What Home shows for each pipeline: totals, the last 7 days against the 7 before, the newest item, and what waits for approval."""
from datetime import datetime, timedelta, timezone

from app.db.mongo import audio_assets, content_pieces, image_assets
from tests.conftest import create_workspace, signup_new_user

NOW = datetime.now(timezone.utc)


def _days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


async def test_each_pipeline_reports_real_counts_and_ignores_deleted_archived_and_other_workspaces(api_client):
    await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Summary WS")
    other = "someone-elses-workspace"
    headers = {"X-Workspace-Id": ws}

    await content_pieces.insert_many([
        {"workspace_id": ws, "piece_id": "t1", "created_at": _days_ago(1), "approval_status": "pending"},
        {"workspace_id": ws, "piece_id": "t2", "created_at": _days_ago(3), "approval_status": "approved"},
        {"workspace_id": ws, "piece_id": "t3", "created_at": _days_ago(10), "approval_status": "approved"},
        {"workspace_id": ws, "piece_id": "t4", "created_at": _days_ago(1), "deleted": True},
        {"workspace_id": ws, "piece_id": "t5", "created_at": _days_ago(1), "archived": True},
        {"workspace_id": other, "piece_id": "t6", "created_at": _days_ago(1)},
    ])
    await audio_assets.insert_many([
        {"id": "a1", "workspace_id": ws, "created_at": _days_ago(2), "approval_status": "pending",
         "video_clips": [{"id": "v1", "created_at": _days_ago(1)}, {"id": "v2", "created_at": _days_ago(9)}]},
        {"id": "a2", "workspace_id": ws, "created_at": _days_ago(20), "approval_status": "approved"},
    ])
    await image_assets.insert_many([
        {"id": "i1", "workspace_id": ws, "created_at": _days_ago(1), "approval_status": "approved"},
        {"id": "i2", "workspace_id": ws, "created_at": _days_ago(2), "approval_status": "pending", "replaced_by": "i1"},
    ])
    try:
        res = await api_client.get("/api/v1/analytics/pipeline-summary", headers=headers)
        assert res.status_code == 200, res.text
        body = res.json()

        assert body["text"]["total"] == 3 and body["text"]["this_week"] == 2 and body["text"]["prior_week"] == 1
        assert body["text"]["pending_approval"] == 1 and body["text"]["last_created_at"].endswith("Z")

        assert body["audio"]["total"] == 2 and body["audio"]["this_week"] == 1 and body["audio"]["pending_approval"] == 1

        assert body["image"]["total"] == 1 and body["image"]["this_week"] == 1  # the replaced one is not counted

        assert body["video"]["total"] == 2 and body["video"]["this_week"] == 1 and body["video"]["prior_week"] == 1
    finally:
        for collection in (content_pieces, audio_assets, image_assets):
            await collection.delete_many({"workspace_id": {"$in": [ws, other]}})


async def test_a_workspace_with_nothing_made_returns_zeros_not_errors(api_client):
    await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Empty Summary WS")
    body = (await api_client.get("/api/v1/analytics/pipeline-summary", headers={"X-Workspace-Id": ws})).json()
    for kind in ("text", "audio", "image", "video"):
        assert body[kind]["total"] == 0 and body[kind]["last_created_at"] is None
