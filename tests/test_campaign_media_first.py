"""Media-first campaigns: the source is a video the member already has. Its words are the topic, each day gets its own part of it,
cut as an editable project with its own captions, and attached to that day's posts. Nothing is generated."""
from uuid import uuid4

import pytest

from app.db.mongo import content_pieces, get_db, media_assets
from app.pipelines.campaigns import media as campaign_media
from app.pipelines.campaigns import sources
from app.pipelines.publish import attachments
from app.video import service


def test_the_video_is_shared_evenly_across_the_days_and_a_long_share_is_cut_short():
    assert sources.window_for_day(70, 0, 7) == (0.0, 10.0)
    assert sources.window_for_day(70, 3, 7) == (30.0, 10.0)
    assert sources.window_for_day(1400, 2, 7) == (400.0, sources.MAX_CUT_SECONDS)
    assert sources.window_for_day(0, 0, 7)[1] == 0.0


async def _video(ws_id: str, *, transcript: bool = True, seconds: float = 70.0) -> str:
    mid = uuid4().hex
    await media_assets.insert_one({"id": mid, "workspace_id": ws_id, "kind": "video", "url": f"https://cdn.example/{mid}.mp4",
                                   "mime_type": "video/mp4", "duration_s": seconds, "title": "Founder talk"})
    if transcript:
        tid = uuid4().hex
        await get_db()["video_transcripts"].insert_one({"id": tid, "workspace_id": ws_id, "media_id": mid, "status": "done", "duration_s": seconds})
        words = [{"w": f"word{i}", "s_ms": i * 1000, "e_ms": i * 1000 + 800} for i in range(int(seconds))]
        await get_db()["video_transcript_pages"].insert_one({"transcript_id": tid, "page": 0, "words": words})
    return mid


async def test_a_video_with_a_transcript_gives_its_words_as_the_topic():
    ws_id = f"ws-{uuid4()}"
    mid = await _video(ws_id)

    topic, ref = await sources.topic_from_video(mid, ws_id)

    assert topic.startswith("word0 word1") and ref["type"] == "video" and ref["id"] == mid and ref["duration_s"] == 70.0


async def test_a_video_without_a_transcript_or_from_another_workspace_says_why():
    ws_id = f"ws-{uuid4()}"
    bare = await _video(ws_id, transcript=False)
    other = await _video(f"ws-{uuid4()}")

    with pytest.raises(sources.SourceError, match="no transcript yet"):
        await sources.topic_from_video(bare, ws_id)
    with pytest.raises(sources.SourceError, match="not found"):
        await sources.topic_from_video(other, ws_id)


async def test_a_day_gets_its_own_cut_with_its_own_captions_rendered_and_attached(monkeypatch):
    ws_id = f"ws-{uuid4()}"
    mid = await _video(ws_id)
    piece = {"piece_id": f"p-{uuid4()}", "workspace_id": ws_id, "platform": "LinkedIn", "content": "Day three post.", "deleted": False}
    await content_pieces.insert_one(dict(piece))
    rendered, attached = [], []

    async def render(**kw):
        rendered.append(kw)
        return {"media_id": "m"}

    async def resolve(workspace_id, asset_type, asset_id=None, **kw):
        return {"asset_type": asset_type, "asset_id": asset_id}

    async def attach(p, workspace_id, user_id, resolved):
        attached.append((p["piece_id"], resolved["asset_type"]))

    monkeypatch.setattr(service, "render_project_and_wait", render)
    monkeypatch.setattr(attachments, "resolve_asset", resolve)
    monkeypatch.setattr(attachments, "attach", attach)
    campaign = {"id": "c-1", "name": "Talk", "source_ref": {"type": "video", "id": mid, "duration_s": 70.0}, "media_plan": {"video": {"aspect": "9:16"}}}

    project_id = await campaign_media.attach_source_cut(campaign, [piece["piece_id"]], 3, 7, workspace_id=ws_id, user_id="u1")

    project = await get_db()["video_projects"].find_one({"id": project_id})
    clip = project["edit"]["video"][0]
    assert clip["media_id"] == mid and clip["src_in_f"] == 900 and clip["dur_f"] == 300 and clip["audio"] is True      # 30 s in, 10 s long
    words = project["edit"]["captions"]["words"]
    assert words[0]["w"] == "word30" and words[0]["s_ms"] == 0 and words[-1]["w"] == "word39"                         # this day's words only
    assert project["campaign_id"] == "c-1" and project["created_from"]["day"] == 4
    assert len(rendered) == 1 and attached == [(piece["piece_id"], "project")]


async def test_a_source_video_that_is_gone_makes_no_cut():
    out = await campaign_media.attach_source_cut(
        {"id": "c-2", "source_ref": {"type": "video", "id": "missing"}}, ["p"], 0, 7, workspace_id=f"ws-{uuid4()}", user_id="u1",
    )
    assert out is None


async def test_the_source_list_marks_which_videos_can_be_used_now(signup_user):
    from tests.conftest import create_workspace

    client, _ = await signup_user()
    ws_id = await create_workspace(client, f"Sources {uuid4().hex[:6]}")
    ready = await _video(ws_id)
    bare = await _video(ws_id, transcript=False)

    body = (await client.get("/api/v1/campaigns/video-sources", headers={"X-Workspace-Id": ws_id})).json()

    by_id = {i["id"]: i for i in body["items"]}
    assert by_id[ready]["usable"] is True and by_id[ready]["reason"] is None
    assert by_id[bare]["usable"] is False and "Transcribe" in by_id[bare]["reason"]
