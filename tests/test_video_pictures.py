"""A slideshow project from pictures, with an optional voice and music, through the builder and the project API."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1 import video_projects as api
from app.db.mongo import media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.video import builders, edl


def _call(fn, *args, **kwargs):
    return getattr(fn, "__wrapped__", fn)(*args, **kwargs)


def _ctx(ws=None):
    return SimpleNamespace(workspace_id=ws or f"ws-{uuid4()}", user_id="u1")


async def _media(ws: str, kind: str, duration: float | None = None, **kw) -> str:
    row = MediaAsset(
        id=str(uuid4()), workspace_id=ws, kind=MediaKind(kind), url=f"https://res.cloudinary.com/x/{kind}/upload/v1/{uuid4()}",
        mime_type="image/jpeg" if kind == "image" else "audio/mpeg", duration_s=duration, source=kw.pop("source", MediaSource.UPLOADED),
        created_by="u1", created_at=datetime.now(timezone.utc),
    )
    await media_assets.insert_one(row.model_dump())
    return row.id


def test_pictures_share_the_voice_length_exactly_and_the_last_one_is_not_cut_off():
    edit = builders.from_pictures(picture_media_ids=["a", "b", "c"], voice_media_id="v", voice_seconds=10.0, music_media_id="m")
    lengths = [c["dur_f"] for c in edit["video"]]
    assert sum(lengths) == 300 and max(lengths) - min(lengths) <= 1
    assert edit["duration_f"] == 300 and edit["audio"][0]["role"] == "voice" and edit["audio"][0]["dur_f"] == 300
    music = next(a for a in edit["audio"] if a["role"] == "music")
    assert music["duck"] is True and music["gain_db"] < 0           # the bed gives way under the voice
    assert all(c["motion"] is True for c in edit["video"])           # every picture has the slow zoom


def test_without_a_voice_each_picture_stays_for_the_time_asked_for():
    edit = builders.from_pictures(picture_media_ids=["a", "b"], seconds_per_picture=4.0)
    assert [c["dur_f"] for c in edit["video"]] == [120, 120] and edit["duration_f"] == 240 and edit["audio"] == []
    assert edit["video"][1]["at_f"] == 120


@pytest.mark.parametrize("kwargs,words", [
    ({"picture_media_ids": []}, "at least one"),
    ({"picture_media_ids": ["a"] * 61}, "at most 60"),
    ({"picture_media_ids": ["a"], "seconds_per_picture": 0.2}, "seconds"),
    ({"picture_media_ids": ["a"], "voice_media_id": "v", "voice_seconds": 0}, "voice's length"),
])
def test_a_slideshow_that_cannot_be_made_says_why(kwargs, words):
    with pytest.raises(edl.EditError) as caught:
        builders.from_pictures(**kwargs)
    assert words in str(caught.value)


async def test_the_project_api_builds_a_slideshow_from_library_pictures_and_a_voice():
    ctx = _ctx()
    pics = [await _media(ctx.workspace_id, "image") for _ in range(3)]
    voice = await _media(ctx.workspace_id, "audio", duration=9.0, source=MediaSource.SYNTHESIZED)
    out = await _call(api.create_project, None, api.CreateBody(source="pictures", picture_media_ids=pics, voice_media_id=voice, title="Launch"), ctx)
    assert out["edit"]["duration_f"] == 270 and len(out["edit"]["video"]) == 3
    assert out["media"][voice]["origin"] == "ai"                       # a synthesized voice is flagged AI generated
    assert out["origin"]["origin"] == "mixed" and out["origin"]["contains_ai"] is True


async def test_a_slideshow_refuses_a_file_that_is_not_a_picture_or_not_in_the_workspace():
    ctx = _ctx()
    pic = await _media(ctx.workspace_id, "image")
    sound = await _media(ctx.workspace_id, "audio", duration=5.0)
    with pytest.raises(HTTPException) as caught:
        await _call(api.create_project, None, api.CreateBody(source="pictures", picture_media_ids=[pic, sound]), ctx)
    assert caught.value.status_code == 400 and "pictures" in caught.value.detail
    other = await _media(f"ws-{uuid4()}", "image")
    with pytest.raises(HTTPException) as caught:
        await _call(api.create_project, None, api.CreateBody(source="pictures", picture_media_ids=[other]), ctx)
    assert caught.value.status_code == 404


async def test_the_slow_zoom_reaches_the_worker_job():
    ctx = _ctx()
    pic = await _media(ctx.workspace_id, "image")
    out = await _call(api.create_project, None, api.CreateBody(source="pictures", picture_media_ids=[pic]), ctx)
    media = {pic: {"url": "https://res.cloudinary.com/x/image/upload/v1/a.jpg", "kind": "image"}}
    job = edl.to_job(out["edit"], media)
    assert job["edit"]["video"][0]["motion"] is True and job["inputs"]["m0"]["kind"] == "image"
