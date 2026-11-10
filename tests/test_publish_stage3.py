"""Stage 3 publishing: translated subtitle tracks go up with a YouTube video, and the review step leaves a record when it is switched.
No real YouTube call is made: the web client is a stand-in."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.db.mongo import get_db
from app.pipelines.publish.youtube import publisher as publisher_module
from tests.conftest import create_workspace, signup_new_user

PUBLISHER = publisher_module.YouTubePublisher


class Http:
    """Records every subtitle upload. `outcomes` is a language code mapped to a status code or "raise"; the rest are accepted."""

    def __init__(self, outcomes=None):
        self.outcomes, self.calls = outcomes or {}, []

    async def post(self, url, params=None, headers=None, content=None):
        text = content.decode()
        code = re.search(r'"language": "([^"]+)"', text).group(1)
        name = re.search(r'"name": "([^"]*)"', text).group(1)
        self.calls.append({"code": code, "name": name, "body": text, "headers": headers})
        outcome = self.outcomes.get(code, 200)
        if outcome == "raise":
            raise RuntimeError("network")
        resp = MagicMock()
        resp.status_code = outcome
        resp.is_success = 200 <= outcome < 300
        return resp


async def _track(ws_id, media_id, code, name, *, cues=None):
    await get_db()["video_caption_tracks"].update_one(
        {"workspace_id": ws_id, "media_id": media_id, "language": code},
        {"$set": {"workspace_id": ws_id, "media_id": media_id, "language": code, "language_name": name, "created_at": datetime.now(timezone.utc),
                  "cues": cues if cues is not None else [{"start_ms": 1000, "end_ms": 2500, "text": f"Hello in {name}"}, {"start_ms": 3000, "end_ms": 4000, "text": "Second line"}]}},
        upsert=True,
    )


async def _clean(ws_id):
    await get_db()["video_caption_tracks"].delete_many({"workspace_id": ws_id})


async def _send(http, ws_id, media_id, original="en"):
    return await PUBLISHER()._upload_translated_tracks(http, "tok", "vid-1", ws_id, media_id, original)


async def test_each_translation_the_member_made_goes_up_as_its_own_subtitle_track():
    ws, media = "ws-tracks-1", "m-1"
    await _clean(ws)
    await _track(ws, media, "hi", "Hindi")
    await _track(ws, media, "ta", "Tamil")
    http = Http()
    assert await _send(http, ws, media) is None
    assert sorted(c["code"] for c in http.calls) == ["hi", "ta"]
    hindi = next(c for c in http.calls if c["code"] == "hi")
    assert hindi["name"] == "Hindi" and hindi["headers"]["Authorization"] == "Bearer tok"
    assert "00:00:01,000 --> 00:00:02,500" in hindi["body"] and "Hello in Hindi" in hindi["body"] and '"videoId": "vid-1"' in hindi["body"]
    await _clean(ws)


async def test_the_language_the_video_is_spoken_in_and_other_videos_and_other_workspaces_are_left_out():
    ws, media = "ws-tracks-2", "m-2"
    await _clean(ws)
    await _clean("ws-tracks-other")
    await _track(ws, media, "en", "English")           # the original language already has its own track
    await _track(ws, media, "es", "Spanish")
    await _track(ws, "m-other-video", "fr", "French")  # another video
    await _track("ws-tracks-other", media, "de", "German")  # another workspace
    http = Http()
    assert await _send(http, ws, media, original="en-US") is None
    assert [c["code"] for c in http.calls] == ["es"]
    await _clean(ws)
    await _clean("ws-tracks-other")


async def test_a_translation_with_no_lines_is_skipped_and_nothing_is_said():
    ws, media = "ws-tracks-3", "m-3"
    await _clean(ws)
    await _track(ws, media, "fr", "French", cues=[])
    http = Http()
    assert await _send(http, ws, media) is None and http.calls == []
    await _clean(ws)


async def test_one_track_failing_never_stops_the_others_and_is_named():
    ws, media = "ws-tracks-4", "m-4"
    await _clean(ws)
    for code, name in (("hi", "Hindi"), ("ta", "Tamil"), ("te", "Telugu")):
        await _track(ws, media, code, name)
    http = Http({"ta": 500, "te": "raise"})
    note = await _send(http, ws, media)
    assert len(http.calls) == 3, "every track was tried"
    assert note and "Tamil" in note and "Telugu" in note and "Hindi" not in note and "YouTube Studio" in note and "video is up" in note
    await _clean(ws)


async def test_a_connection_that_may_not_add_subtitles_says_reconnect_and_stops():
    ws, media = "ws-tracks-5", "m-5"
    await _clean(ws)
    for code, name in (("hi", "Hindi"), ("ta", "Tamil")):
        await _track(ws, media, code, name)
    http = Http({"hi": 403, "ta": 403})
    note = await _send(http, ws, media)
    assert "Reconnect YouTube in Settings" in note and "/api/" not in note
    assert len(http.calls) == 1, "no point asking again for the rest"
    await _clean(ws)


async def test_at_most_ten_extra_tracks_go_up_and_the_rest_are_named():
    ws, media = "ws-tracks-6", "m-6"
    await _clean(ws)
    codes = ["hi", "ta", "te", "kn", "ml", "bn", "mr", "gu", "pa", "ur", "fr", "de"]
    for code in codes:
        await _track(ws, media, code, code.upper())
    http = Http()
    note = await _send(http, ws, media)
    assert len(http.calls) == publisher_module.MAX_EXTRA_TRACKS == 10
    assert note and note.count(",") >= 1, "the two left over are named"
    await _clean(ws)


async def test_the_original_track_still_reports_the_same_way_after_the_change():
    words = [{"word": "Hello", "start_s": 0.0, "end_s": 0.5}, {"word": "world", "start_s": 0.5, "end_s": 1.0}]
    media = {"transcript": words, "transcript_language": "english"}
    ok = Http()
    assert await PUBLISHER()._upload_captions(ok, "tok", "vid-1", media) is None and ok.calls[0]["code"] == "en"
    assert "Reconnect YouTube in Settings" in await PUBLISHER()._upload_captions(Http({"en": 403}), "tok", "vid-1", media)
    assert "couldn't be added" in await PUBLISHER()._upload_captions(Http({"en": 500}), "tok", "vid-1", media)
    assert "couldn't be added" in await PUBLISHER()._upload_captions(Http({"en": "raise"}), "tok", "vid-1", media)
    assert await PUBLISHER()._upload_captions(Http(), "tok", "vid-1", {}) is None


# ---- the review step leaves a record -----------------------------------------------------------------------------------------------------
async def test_switching_the_review_step_leaves_a_row_in_activity_and_an_unchanged_setting_leaves_none(api_client, monkeypatch):
    told = []

    async def record(**kwargs):
        told.append(kwargs)

    monkeypatch.setattr("app.shared.activity.projector.record_system", record)
    await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Review record", tier="large")

    off = await api_client.patch(f"/api/v1/workspaces/{ws}", json={"require_review": False})
    assert off.status_code == 200 and off.json()["policy"]["review_required"] is False
    assert len(told) == 1 and told[0]["metadata"] == {"event": "workspace.review_setting", "require_review": False}
    assert told[0]["workspace_id"] == ws and told[0]["category"] == "workspace_alert" and told[0]["actor_user_id"]
    assert told[0]["title"]["$t"] == "Review step switched off"

    await api_client.patch(f"/api/v1/workspaces/{ws}", json={"require_review": False})
    assert len(told) == 1, "the same value again is not news"
    await api_client.patch(f"/api/v1/workspaces/{ws}", json={"require_review": True})
    await api_client.patch(f"/api/v1/workspaces/{ws}", json={"require_review": None})
    assert [t["title"]["$t"] for t in told] == ["Review step switched off", "Review step switched on", "Review step back to the plan's default"]
    await api_client.patch(f"/api/v1/workspaces/{ws}", json={"language": "en"})
    assert len(told) == 3, "other settings do not leave a review row"


async def test_a_failing_activity_row_never_fails_the_setting(api_client, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("activity is down")

    monkeypatch.setattr("app.shared.activity.projector.record_system", boom)
    await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Review record 2", tier="large")
    res = await api_client.patch(f"/api/v1/workspaces/{ws}", json={"require_review": False})
    assert res.status_code == 200 and res.json()["policy"]["review_required"] is False
