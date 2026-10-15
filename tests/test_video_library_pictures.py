"""The picture picker: a workspace sees its own pictures with where each came from, newest first, and never another workspace's."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db.mongo import media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from tests.conftest import create_workspace


async def _picture(ws: str, minutes: int, **extra) -> dict:
    row = MediaAsset(
        id=str(uuid4()), workspace_id=ws, kind=MediaKind.IMAGE, url=f"https://res.cloudinary.com/democloud/image/upload/v1/{uuid4()}.png",
        mime_type="image/png", width=800, height=600, source=MediaSource.UPLOADED, created_by="u1",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes),
    ).model_dump()
    row.update(extra)
    await media_assets.insert_one(dict(row))
    return row


async def test_a_workspace_sees_its_own_pictures_newest_first_with_their_origin(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Pictures")
    other_client, _ = await signup_user()
    other = await create_workspace(other_client, "Other")
    old = await _picture(ws, 30)
    new = await _picture(ws, 1, origin="ai", contains_ai=True)
    await _picture(other, 5)
    res = await client.get("/api/v1/video-media/pictures", headers={"X-Workspace-Id": ws})
    assert res.status_code == 200, res.text
    pictures = res.json()["pictures"]
    assert [p["id"] for p in pictures][:2] == [new["id"], old["id"]]
    assert pictures[0]["origin"] == "ai" and pictures[0]["contains_ai"] is True
    assert all(p["url"].startswith("https://") for p in pictures)
    assert len({p["id"] for p in pictures}) == len(pictures)
    seen_other = await other_client.get("/api/v1/video-media/pictures", headers={"X-Workspace-Id": other})
    assert new["id"] not in [p["id"] for p in seen_other.json()["pictures"]]
