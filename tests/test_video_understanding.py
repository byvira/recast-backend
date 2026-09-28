"""Tests for real video/audio understanding: transcript -> real chapters ->
captions -> YouTube description, plus agents being able to read a video.

The bugs these pin down: timestamps/chapters were generic because the "add
timestamps" chip asked a model to "space sections approximately evenly" from
the post text; the YouTube description was the raw post text; there were no
captions; uploaded media kept none of Cloudinary's own measurements; and no
agent could read anything a member uploaded.

Real Mongo, real routes. Only the outside world is stubbed: Cloudinary
(upload + audio-track fetch), Groq Whisper, and the chapter/metadata LLM.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from app.agents.personal.history import iter_member_content
from app.api.v1 import media as media_module
from app.db.mongo import brand_profiles, content_pieces, media_assets, workspace_ai_budgets, workspace_ai_usage_daily
from app.models.audio_asset import TranscriptWord
from app.models.media import MediaAsset, MediaChapter, MediaKind, MediaSource, MediaTranscriptWord
from app.pipelines.media import video_analysis as va
from app.pipelines.media.captions import build_cues, language_code, to_srt, to_vtt
from app.pipelines.publish.token_store import save_token
from app.pipelines.publish.youtube import metadata as metadata_module
from app.pipelines.publish.youtube import publisher as publisher_module
from app.pipelines.publish.youtube.links import clean_url, collect_social_links
from app.pipelines.publish.youtube.metadata import build_description
from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from tests.conftest import create_workspace

_VIDEO_URL = "https://res.cloudinary.com/demo/video/upload/v1/recast/video/u/clip.mp4"


def _words(blocks: int = 9, block_s: float = 10.0) -> list[MediaTranscriptWord]:
    """A 90s recording: one 5-word sentence every 10s, so segment starts are
    real, known positions (0, 10, 20 ... 80)."""
    out: list[MediaTranscriptWord] = []
    for b in range(blocks):
        base = b * block_s
        for k, w in enumerate(["Now", "we", "cover", f"topic{b}", "properly."]):
            out.append(MediaTranscriptWord(word=w, start_s=base + k * 0.8, end_s=base + k * 0.8 + 0.6))
    return out


def _segments(words=None) -> list[va.Segment]:
    return va.words_to_segments(words or _words())


# ── timestamps and Cloudinary URL derivatives ───────────────────────────────

def test_timestamps_round_trip_in_the_form_youtube_parses():
    assert va.format_timestamp(0) == "0:00"
    assert va.format_timestamp(75) == "1:15"
    assert va.format_timestamp(3725) == "1:02:05"
    assert va.parse_timestamp("1:15") == 75.0
    assert va.parse_timestamp("1:02:05") == 3725.0
    assert va.parse_timestamp(42) == 42.0
    assert va.parse_timestamp("abc") is None
    assert va.parse_timestamp("1:2:3:4") is None
    assert va.parse_timestamp(None) is None


def test_the_audio_track_is_the_same_cloudinary_file_as_mp3_and_the_poster_a_frame():
    assert va.audio_track_url(_VIDEO_URL).endswith("/clip.mp3")
    assert "/upload/" in va.audio_track_url(_VIDEO_URL)
    assert va.audio_track_url("https://example.com/clip.mp4") == "https://example.com/clip.mp4"  # not Cloudinary

    poster = va.poster_url_for(_VIDEO_URL)
    assert poster.endswith("/clip.jpg") and "/so_1," in poster
    assert va.poster_url_for("https://res.cloudinary.com/demo/image/upload/v1/x.png") is None


def test_words_become_segments_that_keep_their_real_start_times():
    segs = _segments()
    assert [s.start_s for s in segs] == [0, 10, 20, 30, 40, 50, 60, 70, 80]
    assert segs[0].text == "Now we cover topic0 properly."


# ── chapters: never invented ────────────────────────────────────────────────

def _raw(*pairs):
    return [{"start": s, "title": t} for s, t in pairs]


def test_chapters_land_on_real_segment_starts_and_open_at_zero():
    chapters = va.validate_chapters(
        _raw(("0:10", "Opening idea"), ("0:30", "The middle"), ("1:10", "Wrapping up")), _segments(), 90.0,
    )
    assert [c.start_s for c in chapters] == [0.0, 30.0, 70.0]  # first pinned to 0:00 (YouTube's rule)
    assert [c.title for c in chapters] == ["Opening idea", "The middle", "Wrapping up"]


def test_a_time_the_model_invented_is_rejected_not_rounded_into_place():
    chapters = va.validate_chapters(
        _raw(("0:00", "Start"), ("0:20", "Real"), ("0:50", "Real too"), ("9:59", "Made up")), _segments(), 90.0,
    )
    assert [c.title for c in chapters] == ["Start", "Real", "Real too"]  # the 9:59 one is gone


def test_chapters_closer_than_ten_seconds_are_dropped():
    words = [MediaTranscriptWord(word=f"w{i}", start_s=float(i * 4), end_s=i * 4 + 0.5) for i in range(30)]
    segs = va.words_to_segments(words, max_span_s=3.0)  # a segment every 4s
    chapters = va.validate_chapters(
        _raw(("0:00", "A"), ("0:04", "Too close"), ("0:12", "B"), ("0:16", "Too close 2"), ("0:24", "C")),
        segs, 120.0,
    )
    starts = [c.start_s for c in chapters]
    assert all(b - a >= 10 for a, b in zip(starts, starts[1:]))
    assert [c.title for c in chapters] == ["A", "B", "C"]


def test_fewer_than_three_real_chapters_means_none_never_padding():
    assert va.validate_chapters(_raw(("0:00", "Only"), ("0:30", "Two")), _segments(), 90.0) == []


def test_a_video_under_thirty_seconds_gets_no_chapters():
    words = _words(blocks=2)
    assert va.validate_chapters(_raw(("0:00", "A"), ("0:10", "B"), ("0:20", "C")), va.words_to_segments(words), 25.0) == []


def test_chapters_are_refused_when_speech_only_starts_late():
    """YouTube needs the first chapter at 0:00; if nobody speaks until 0:40 an
    honest chapter list can't start there."""
    words = [MediaTranscriptWord(word="hi.", start_s=40.0 + i * 10, end_s=40.5 + i * 10) for i in range(6)]
    segs = va.words_to_segments(words)
    assert va.validate_chapters(_raw(("0:40", "A"), ("0:50", "B"), ("1:00", "C")), segs, 120.0) == []


def test_chapter_titles_are_cleaned():
    chapters = va.validate_chapters(
        _raw(("0:00", '0:00 - "Intro to the plan"'), ("0:20", "x" * 200), ("0:50", "  Closing thoughts. ")),
        _segments(), 90.0,
    )
    assert chapters[0].title == "Intro to the plan"
    assert len(chapters[1].title) <= 60
    assert chapters[2].title == "Closing thoughts"


def test_malformed_model_output_is_survived():
    assert va.validate_chapters(None, _segments(), 90.0) == []
    assert va.validate_chapters("nope", _segments(), 90.0) == []
    assert va.validate_chapters([{"title": "no start"}, "junk", {"start": "0:10"}], _segments(), 90.0) == []
    assert va.validate_chapters(_raw(("0:00", "A")), [], 90.0) == []


def test_the_chapter_block_is_the_exact_form_youtube_turns_into_chapters():
    block = va.chapters_block([MediaChapter(start_s=0, title="Intro"), MediaChapter(start_s=75, title="Demo")])
    assert block == "0:00 Intro\n1:15 Demo"


async def test_chapter_generation_uses_only_real_positions_and_survives_an_llm_failure(monkeypatch):
    prompts: list[str] = []

    async def _llm(prompt, **kw):
        prompts.append(prompt)
        return {"chapters": _raw(("0:00", "Hook"), ("0:30", "Body"), ("1:00", "Close"))}

    monkeypatch.setattr(va, "call_llm_structured", _llm)
    chapters = await va.generate_chapters(_words(), "english", 90.0)
    assert [c.start_s for c in chapters] == [0.0, 30.0, 60.0]
    assert "[0:30] Now we cover topic3 properly." in prompts[0]  # the model sees real timed transcript lines

    async def _boom(prompt, **kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(va, "call_llm_structured", _boom)
    assert await va.generate_chapters(_words(), "english", 90.0) == []


# ── analysing a recording end to end (Cloudinary + Whisper + LLM stubbed) ────

class _FakeHttp:
    """Stands in for httpx.AsyncClient inside video_analysis."""

    def __init__(self, payload: bytes = b"mp3-bytes", fail: bool = False):
        self.payload, self.fail, self.urls = payload, fail, []

    def __call__(self, *a, **kw):
        outer = self

        class _Client:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            async def get(self_inner, url, **k):
                outer.urls.append(url)
                if outer.fail:
                    raise RuntimeError("cloudinary unreachable")
                resp = MagicMock()
                resp.content = outer.payload
                resp.raise_for_status = lambda: None
                return resp

        return _Client()


def _video_doc(**over) -> dict:
    doc = {"id": "m1", "kind": "video", "url": _VIDEO_URL, "duration_s": 90.0}
    doc.update(over)
    return doc


def _stub_transcription(monkeypatch, words=None, language="english"):
    source = _words() if words is None else words  # [] means silence, not "use the default"
    tw = [TranscriptWord(word=w.word, start_s=w.start_s, end_s=w.end_s) for w in source]

    async def _fake(audio, filename, language_arg=None, *a, **k):
        return (tw, language) if tw else ([], None)

    monkeypatch.setattr(va, "transcribe_audio_detailed", _fake)


async def test_analysis_transcribes_the_audio_track_and_builds_real_chapters(monkeypatch):
    http = _FakeHttp()
    monkeypatch.setattr(va.httpx, "AsyncClient", http)
    _stub_transcription(monkeypatch)

    async def _llm(prompt, **kw):
        return {"chapters": _raw(("0:00", "Hook"), ("0:30", "Body"), ("1:00", "Close"))}

    monkeypatch.setattr(va, "call_llm_structured", _llm)

    update = await va.analyze_media(_video_doc())

    assert http.urls == [va.audio_track_url(_VIDEO_URL)]  # fetched the mp3 derivative, not the video
    assert update["analysis_status"] == "done"
    assert update["transcript_language"] == "english"
    assert len(update["transcript"]) == 45
    assert [c["title"] for c in update["chapters"]] == ["Hook", "Body", "Close"]
    assert update["poster_url"] and update["poster_url"].endswith(".jpg")


async def test_a_recording_too_big_to_transcribe_says_so(monkeypatch):
    monkeypatch.setattr(va.httpx, "AsyncClient", _FakeHttp(payload=b"x" * 50))
    monkeypatch.setattr(va, "MAX_TRANSCRIBE_BYTES", 10)
    update = await va.analyze_media(_video_doc())
    assert update["analysis_status"] == "failed"
    assert "too long" in update["analysis_error"]


async def test_silence_and_unreachable_files_fail_with_a_plain_reason(monkeypatch):
    monkeypatch.setattr(va.httpx, "AsyncClient", _FakeHttp())
    _stub_transcription(monkeypatch, words=[])
    silent = await va.analyze_media(_video_doc())
    assert silent["analysis_status"] == "failed" and "No speech" in silent["analysis_error"]

    monkeypatch.setattr(va.httpx, "AsyncClient", _FakeHttp(fail=True))
    unreachable = await va.analyze_media(_video_doc())
    assert unreachable["analysis_status"] == "failed" and "audio" in unreachable["analysis_error"]
    assert "cloudinary" not in unreachable["analysis_error"].lower()  # no internals in user-facing copy


async def test_a_chapter_failure_keeps_the_transcript(monkeypatch):
    monkeypatch.setattr(va.httpx, "AsyncClient", _FakeHttp())
    _stub_transcription(monkeypatch)

    async def _boom(prompt, **kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(va, "call_llm_structured", _boom)
    update = await va.analyze_media(_video_doc())
    assert update["analysis_status"] == "done"
    assert update["chapters"] == [] and len(update["transcript"]) == 45


# ── captions ────────────────────────────────────────────────────────────────

def test_captions_are_short_readable_and_never_overlap():
    words = _words()
    cues = build_cues(words)
    assert cues
    for a, b in zip(cues, cues[1:]):
        assert a.end_s <= b.start_s + 1e-9
    for c in cues:
        assert c.end_s > c.start_s
        assert c.end_s - c.start_s <= 6.0 + 0.6
        lines = c.text.split("\n")
        assert len(lines) <= 2 and all(len(line) <= 42 for line in lines)


def test_a_long_sentence_wraps_to_two_balanced_lines():
    words = [MediaTranscriptWord(word=w, start_s=i * 0.4, end_s=i * 0.4 + 0.3) for i, w in enumerate(
        "this is a fairly long sentence that clearly cannot fit on a single caption line".split()
    )]
    text = build_cues(words)[0].text
    assert "\n" in text
    first, second = text.split("\n")
    assert abs(len(first) - len(second)) < 20


def test_srt_and_vtt_formats():
    cues = build_cues(_words(blocks=2))
    srt = to_srt(cues)
    assert srt.startswith("1\n00:00:00,000 --> ")
    assert "\n\n2\n" in srt
    vtt = to_vtt(cues)
    assert vtt.startswith("WEBVTT\n\n00:00:00.000 --> ")
    assert to_srt([]) == "" and to_vtt([]).startswith("WEBVTT")


def test_language_names_map_to_youtube_codes():
    assert language_code("english") == "en"
    assert language_code("Tamil") == "ta"
    assert language_code("en") == "en"
    assert language_code("klingon") is None
    assert language_code(None) is None


# ── the YouTube description ─────────────────────────────────────────────────

_CH = [MediaChapter(start_s=0, title="Intro"), MediaChapter(start_s=30, title="Demo"), MediaChapter(start_s=60, title="Wrap")]


def test_the_description_gets_real_chapters_and_the_placeholder_line_goes():
    body = "A great video.\n\nTimestamps: [ADD AFTER EDITING YOUR VIDEO]\n\nSubscribe!"
    out = build_description(body, _CH, [("Instagram", "https://instagram.com/acme")])
    assert "[ADD AFTER" not in out
    assert "Chapters\n0:00 Intro\n0:30 Demo\n1:00 Wrap" in out
    assert out.endswith("Follow\nInstagram: https://instagram.com/acme")


def test_the_placeholder_is_left_alone_when_there_are_no_real_chapters():
    body = "A video.\nTimestamps: [ADD AFTER EDITING YOUR VIDEO]"
    assert build_description(body, [], []) == body


def test_adding_chapters_twice_does_not_duplicate_them():
    once = build_description("Post text.", _CH)
    twice = build_description(once, _CH)
    assert twice.count("Chapters") == 1
    assert twice == once


def test_the_description_respects_youtubes_limit_and_never_cuts_chapters_or_links():
    out = build_description("x" * 9000, _CH, [("LinkedIn", "https://linkedin.com/company/acme")])
    assert len(out) <= 5000
    assert "Chapters\n0:00 Intro" in out and out.endswith("LinkedIn: https://linkedin.com/company/acme")


# ── social links: only ones that exist ──────────────────────────────────────

def test_only_real_web_addresses_count_as_links():
    assert clean_url("https://instagram.com/acme") == "https://instagram.com/acme"
    assert clean_url("instagram.com/acme") == "https://instagram.com/acme"
    assert clean_url("  ") is None
    assert clean_url("just some words") is None
    assert clean_url("acme") is None
    assert clean_url(None) is None


async def test_links_come_from_the_brand_first_then_connected_accounts_and_only_the_four_that_matter(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Links WS")
    for platform, url in (
        ("instagram", "https://instagram.com/connected"),
        ("facebook", "https://facebook.com/123"),
        ("threads", "https://www.threads.net/@acme"),
        ("bluesky", "https://bsky.app/profile/acme"),   # not one of the four
        ("youtube", "https://www.youtube.com/channel/x"),  # where we're posting
    ):
        await save_token(
            workspace_id=ws_id, platform=platform, access_token="t", refresh_token=None,
            expires_at=None, platform_user_id="u", username="acme", connected_by="", profile_url=url,
        )
    brand = {"visual_identity": {"social_links": {
        "linkedin": "linkedin.com/company/acme",          # typed; LinkedIn can't be auto-detected
        "instagram": "https://instagram.com/typed",       # typed beats the connected account
    }}}

    links = await collect_social_links(ws_id, brand)
    assert links == [
        ("LinkedIn", "https://linkedin.com/company/acme"),
        ("Instagram", "https://instagram.com/typed"),
        ("Facebook", "https://facebook.com/123"),
        ("Threads", "https://www.threads.net/@acme"),
    ]


async def test_no_links_means_no_follow_block(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "No Links WS")
    assert await collect_social_links(ws_id, {}) == []
    assert "Follow" not in build_description("Post.", _CH, await collect_social_links(ws_id, {}))


# ── media upload keeps Cloudinary's measurements ────────────────────────────

def _stub_upload(monkeypatch, **fields):
    async def _fake(contents, content_type, user_id):
        return {"url": _VIDEO_URL, "width": None, "height": None, "duration_s": None, **fields}

    monkeypatch.setattr(media_module, "upload_file_detailed", _fake)


async def _upload(client, ws_id, name, content_type):
    return await client.post(
        "/api/v1/media", files={"file": (name, b"bytes", content_type)}, headers={"X-Workspace-Id": ws_id},
    )


async def test_an_uploaded_video_keeps_its_duration_size_and_a_poster(signup_user, monkeypatch):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Upload WS")
    _stub_upload(monkeypatch, width=1920, height=1080, duration_s=42.5)

    res = await _upload(client, ws_id, "clip.mp4", "video/mp4")
    assert res.status_code == 201, res.text
    body = res.json()
    assert (body["width"], body["height"], body["duration_s"]) == (1920, 1080, 42.5)
    assert body["poster_url"].endswith(".jpg")
    assert body["analysis_status"] == "none"


async def test_images_and_audio_only_keep_what_applies_to_them(signup_user, monkeypatch):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Upload Kinds WS")

    _stub_upload(monkeypatch, width=800, height=600, duration_s=None)
    image = (await _upload(client, ws_id, "p.png", "image/png")).json()
    assert (image["width"], image["height"], image["duration_s"], image["poster_url"]) == (800, 600, None, None)

    _stub_upload(monkeypatch, width=None, height=None, duration_s=12.0)
    audio = (await _upload(client, ws_id, "a.mp3", "audio/mpeg")).json()
    assert (audio["width"], audio["height"], audio["duration_s"]) == (None, None, 12.0)


# ── POST /media/{id}/analyze and GET /media/{id}/captions ────────────────────

async def _seed_media(ws_id: str, user_id: str, **over) -> str:
    doc = MediaAsset(
        id=str(uuid4()), workspace_id=ws_id, kind=MediaKind.VIDEO, url=_VIDEO_URL, mime_type="video/mp4",
        duration_s=90.0, source=MediaSource.UPLOADED, created_by=user_id, created_at=datetime.now(timezone.utc),
    ).model_dump()
    doc.update(over)
    await media_assets.insert_one(doc)
    return doc["id"]


def _analysis_update(status="done") -> dict:
    return {
        "analysis_status": status, "analysis_error": None if status == "done" else "boom",
        "transcript": [w.model_dump() for w in _words()], "transcript_language": "english",
        "chapters": [c.model_dump() for c in _CH],
    }


async def test_analyze_stores_the_result_is_idempotent_and_announces_a_video_once(signup_user, monkeypatch):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Analyze WS")
    media_id = await _seed_media(ws_id, profile["id"])
    runs = AsyncMock(return_value=_analysis_update())
    events = MagicMock()
    monkeypatch.setattr(media_module, "analyze_media", runs)
    monkeypatch.setattr(media_module, "emit_event_background", events)
    url = f"/api/v1/media/{media_id}/analyze"
    headers = {"X-Workspace-Id": ws_id}

    first = await client.post(url, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["analysis_status"] == "done"
    assert len(first.json()["chapters"]) == 3
    stored = await media_assets.find_one({"id": media_id})
    assert stored["transcript_language"] == "english"

    # An already-analysed recording isn't re-run (it costs an LLM call)...
    again = await client.post(url, headers=headers)
    assert again.status_code == 200 and runs.await_count == 1
    # ...unless asked, and a forced re-run must not double-count it for the agents.
    forced = await client.post(url + "?force=true", headers=headers)
    assert forced.status_code == 200 and runs.await_count == 2

    assert events.call_count == 1
    payload = events.call_args.kwargs["payload"]
    assert events.call_args.kwargs["pipeline_type"].value == "video"
    assert payload.content_text.startswith("Now we cover topic0 properly.")


async def test_a_recording_that_cannot_be_read_returns_a_reason_not_an_error(signup_user, monkeypatch):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Analyze Fail WS")
    media_id = await _seed_media(ws_id, profile["id"])
    events = MagicMock()
    monkeypatch.setattr(media_module, "analyze_media", AsyncMock(return_value={
        "analysis_status": "failed", "analysis_error": "No speech could be transcribed from this recording.",
    }))
    monkeypatch.setattr(media_module, "emit_event_background", events)

    res = await client.post(f"/api/v1/media/{media_id}/analyze", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200
    assert res.json()["analysis_status"] == "failed"
    assert "No speech" in res.json()["analysis_error"]
    events.assert_not_called()  # nothing readable exists yet, so nothing to announce


async def test_only_recordings_can_be_analysed_and_scope_and_budget_hold(signup_user, monkeypatch):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Analyze Rules WS")
    headers = {"X-Workspace-Id": ws_id}
    monkeypatch.setattr(media_module, "analyze_media", AsyncMock(return_value=_analysis_update()))
    monkeypatch.setattr(media_module, "emit_event_background", MagicMock())

    image_id = await _seed_media(ws_id, profile["id"], kind="image", mime_type="image/png")
    assert (await client.post(f"/api/v1/media/{image_id}/analyze", headers=headers)).status_code == 400
    assert (await client.post("/api/v1/media/nope/analyze", headers=headers)).status_code == 404

    video_id = await _seed_media(ws_id, profile["id"])
    other_ws = await create_workspace(client, "Analyze Other WS")
    assert (await client.post(f"/api/v1/media/{video_id}/analyze", headers={"X-Workspace-Id": other_ws})).status_code == 404

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    await workspace_ai_budgets.insert_one({"id": ws_id, "workspace_id": ws_id, "monthly_token_budget": 5})
    await workspace_ai_usage_daily.insert_one(
        {"_id": f"{ws_id}:{today}", "id": f"{ws_id}:{today}", "workspace_id": ws_id, "date": today, "tokens_used": 5}
    )
    blocked = await client.post(f"/api/v1/media/{video_id}/analyze", headers=headers)
    assert blocked.status_code == 403 and "AI budget" in blocked.json()["detail"]


async def test_captions_download_as_srt_and_vtt_or_say_there_is_no_transcript(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Captions WS")
    headers = {"X-Workspace-Id": ws_id}
    with_words = await _seed_media(ws_id, profile["id"], **_analysis_update())
    without = await _seed_media(ws_id, profile["id"])

    srt = await client.get(f"/api/v1/media/{with_words}/captions", headers=headers)
    assert srt.status_code == 200
    assert srt.headers["content-type"].startswith("application/x-subrip")
    assert "attachment" in srt.headers["content-disposition"] and srt.headers["content-disposition"].endswith('.srt"')
    assert srt.text.startswith("1\n00:00:00,000 --> ")

    vtt = await client.get(f"/api/v1/media/{with_words}/captions?format=vtt", headers=headers)
    assert vtt.status_code == 200 and vtt.text.startswith("WEBVTT")

    none = await client.get(f"/api/v1/media/{without}/captions", headers=headers)
    assert none.status_code == 404 and "Analyse" in none.json()["detail"]
    assert (await client.get(f"/api/v1/media/{with_words}/captions?format=doc", headers=headers)).status_code == 422


# ── the YouTube review draft (POST /publish/youtube/prepare) ─────────────────

async def _brand(client, ws_id, social=None) -> str:
    res = await client.post("/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id})
    brand_id = res.json()["brand_profile_id"]
    await brand_profiles.update_one({"id": brand_id}, {"$set": {
        "is_complete": True, "visual_identity": {"social_links": social or {}},
    }})
    return brand_id


async def _piece_with_video(client, profile, ws_id, brand_id, media_id) -> str:
    session_id = str(uuid4())
    await ensure_session_exists(
        session_id=session_id, workspace_id=ws_id, user_id=profile["id"], brand_id=brand_id, source_type="text",
    )
    piece_id = await save_live_piece(
        session_id=session_id, workspace_id=ws_id, user_id=profile["id"], brand_id=brand_id, platform="YouTube",
        content="How we plan a launch.\n\nTimestamps: [ADD AFTER EDITING YOUR VIDEO]", word_count=8, char_count=70,
    )
    res = await client.patch(
        f"/api/v1/content/pieces/{piece_id}/media", json={"media_id": media_id}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    return piece_id


async def test_the_review_draft_carries_real_chapters_links_and_a_title_from_the_transcript(signup_user, monkeypatch):
    seen: list[str] = []

    async def _llm(prompt, **kw):
        seen.append(prompt)
        return "TITLE: How to plan a launch\nTAGS: launch, planning, startup"

    monkeypatch.setattr(metadata_module, "call_llm", _llm)
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Prepare WS")
    brand_id = await _brand(client, ws_id, social={"linkedin": "linkedin.com/company/acme"})
    media_id = await _seed_media(ws_id, profile["id"])
    piece_id = await _piece_with_video(client, profile, ws_id, brand_id, media_id)
    # Analysis lands AFTER the piece embedded its snapshot of the video.
    await media_assets.update_one({"id": media_id}, {"$set": _analysis_update()})
    await save_token(
        workspace_id=ws_id, platform="instagram", access_token="t", refresh_token=None, expires_at=None,
        platform_user_id="u", username="acme", connected_by="", profile_url="https://instagram.com/acme",
    )

    # The transcript is analysed AFTER the piece embedded its snapshot of the
    # video; the draft must still see it (it reads the fresh media record).
    res = await client.post(
        "/api/v1/publish/youtube/prepare", json={"piece_id": piece_id}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["title"] == "How to plan a launch"
    assert "<video_transcript>" in seen[0] and "topic3" in seen[0]  # grounded in what's actually said
    assert "Chapters\n0:00 Intro\n0:30 Demo\n1:00 Wrap" in body["description"]
    assert "[ADD AFTER" not in body["description"]
    assert body["description"].endswith(
        "Follow\nLinkedIn: https://linkedin.com/company/acme\nInstagram: https://instagram.com/acme"
    )
    assert body["video"] == {
        "media_id": media_id, "analysis_status": "done", "analysis_error": None,
        "chapter_count": 3, "has_transcript": True, "duration_s": 90.0,
    }
    assert [link["label"] for link in body["social_links"]] == ["LinkedIn", "Instagram"]


async def test_the_draft_still_works_for_a_video_that_was_never_analysed(signup_user, monkeypatch):
    async def _llm(prompt, **kw):
        return "TITLE: A launch\nTAGS: launch"

    monkeypatch.setattr(metadata_module, "call_llm", _llm)
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Prepare Plain WS")
    brand_id = await _brand(client, ws_id)
    media_id = await _seed_media(ws_id, profile["id"])
    piece_id = await _piece_with_video(client, profile, ws_id, brand_id, media_id)

    res = await client.post(
        "/api/v1/publish/youtube/prepare", json={"piece_id": piece_id}, headers={"X-Workspace-Id": ws_id},
    )
    body = res.json()
    assert res.status_code == 200
    assert body["video"]["analysis_status"] == "none" and body["video"]["chapter_count"] == 0
    assert "Chapters" not in body["description"] and "Follow" not in body["description"]
    assert body["social_links"] == []


# ── the "add timestamps" chip ───────────────────────────────────────────────

async def _chip(client, ws_id, piece_id, content="Post text."):
    return await client.post(
        "/api/v1/text/refine",
        json={"content": content, "chip": "add_timestamps", "platform": "YouTube",
              "brand_id": "unused", "piece_id": piece_id},
        headers={"X-Workspace-Id": ws_id},
    )


async def test_the_timestamps_chip_writes_real_chapters_and_nothing_else(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Chip WS")
    brand_id = await _brand(client, ws_id)
    media_id = await _seed_media(ws_id, profile["id"])
    piece_id = await _piece_with_video(client, profile, ws_id, brand_id, media_id)
    await media_assets.update_one({"id": media_id}, {"$set": _analysis_update()})

    res = await _chip(client, ws_id, piece_id, "A post.\nTimestamps: [ADD AFTER EDITING YOUR VIDEO]")
    assert res.status_code == 200, res.text
    refined = res.json()["refined"]
    assert refined.endswith("Chapters\n0:00 Intro\n0:30 Demo\n1:00 Wrap")
    assert "[ADD AFTER" not in refined and res.json()["changed"] is True

    again = await _chip(client, ws_id, piece_id, refined)
    assert again.json()["refined"] == refined  # idempotent


async def test_the_chip_refuses_to_invent_timestamps_without_a_recording(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Chip None WS")
    brand_id = await _brand(client, ws_id)
    session_id = str(uuid4())
    await ensure_session_exists(
        session_id=session_id, workspace_id=ws_id, user_id=profile["id"], brand_id=brand_id, source_type="text",
    )
    piece_id = await save_live_piece(
        session_id=session_id, workspace_id=ws_id, user_id=profile["id"], brand_id=brand_id,
        platform="YouTube", content="No video here.", word_count=3, char_count=14,
    )

    res = await _chip(client, ws_id, piece_id)
    assert res.status_code == 400 and "real recording" in res.json()["detail"]
    assert (await _chip(client, ws_id, None)).status_code == 400


async def test_the_chip_analyses_an_unread_recording_first_and_reports_failures(signup_user, monkeypatch):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Chip Analyse WS")
    brand_id = await _brand(client, ws_id)
    media_id = await _seed_media(ws_id, profile["id"])
    piece_id = await _piece_with_video(client, profile, ws_id, brand_id, media_id)

    failing = AsyncMock(return_value={"analysis_status": "failed", "analysis_error": "No speech could be transcribed."})
    monkeypatch.setattr(va, "analyze_media", failing)
    res = await _chip(client, ws_id, piece_id)
    assert res.status_code == 422 and "No speech" in res.json()["detail"]

    monkeypatch.setattr(va, "analyze_media", AsyncMock(return_value=_analysis_update()))
    ok = await _chip(client, ws_id, piece_id)
    assert ok.status_code == 200 and "0:30 Demo" in ok.json()["refined"]
    assert (await media_assets.find_one({"id": media_id}))["analysis_status"] == "done"  # saved for next time


async def test_the_chip_explains_when_a_recording_is_too_short_for_chapters(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Chip Short WS")
    brand_id = await _brand(client, ws_id)
    media_id = await _seed_media(ws_id, profile["id"], **{**_analysis_update(), "chapters": []})
    piece_id = await _piece_with_video(client, profile, ws_id, brand_id, media_id)

    res = await _chip(client, ws_id, piece_id)
    assert res.status_code == 422 and "too short" in res.json()["detail"]


# ── captions on YouTube itself ──────────────────────────────────────────────

class _CaptionHttp:
    def __init__(self, status=200, raises=False):
        self.status, self.raises, self.calls = status, raises, []

    async def post(self, url, params=None, headers=None, content=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "content": content})
        if self.raises:
            raise RuntimeError("network")
        resp = MagicMock()
        resp.status_code = self.status
        resp.is_success = 200 <= self.status < 300
        return resp


async def _captions(client, media_doc):
    return await publisher_module.YouTubePublisher()._upload_captions(client, "tok", "vid123", media_doc)


async def test_subtitles_are_uploaded_as_srt_in_the_recordings_own_language():
    http = _CaptionHttp()
    note = await _captions(http, _analysis_update())
    assert note is None
    call = http.calls[0]
    assert call["url"].endswith("/captions") and call["params"]["part"] == "snippet"
    body = call["content"].decode()
    assert '"videoId": "vid123"' in body and '"language": "en"' in body
    assert "00:00:00,000 --> " in body and "Now we cover topic0 properly." in body
    assert call["headers"]["Authorization"] == "Bearer tok"


async def test_missing_subtitle_permission_says_reconnect_and_never_fails_the_post():
    note = await _captions(_CaptionHttp(status=403), _analysis_update())
    assert "Reconnect YouTube in Settings" in note and "video is up" in note
    assert "/api/" not in note

    assert "couldn't be added" in await _captions(_CaptionHttp(status=500), _analysis_update())
    assert "couldn't be added" in await _captions(_CaptionHttp(raises=True), _analysis_update())


async def test_subtitles_are_skipped_quietly_or_explained_when_they_cannot_be_made():
    assert await _captions(_CaptionHttp(), {}) is None  # no transcript: nothing to add, nothing to say
    unknown = await _captions(_CaptionHttp(), {**_analysis_update(), "transcript_language": "klingon"})
    assert "language" in unknown and "YouTube Studio" in unknown


# ── agents can read all four kinds of content ───────────────────────────────

async def _seed(collection, ws_id, user_id, **doc):
    await collection.insert_one({
        "workspace_id": ws_id, "created_by": user_id, "created_at": datetime.now(timezone.utc),
        "deleted": False, **doc,
    })


async def test_remy_reads_text_audio_image_and_video_history(signup_user):
    from app.db.mongo import audio_assets, image_assets

    client, profile = await signup_user()
    ws_id = await create_workspace(client, "History WS")
    uid = profile["id"]

    await content_pieces.insert_one({
        "piece_id": str(uuid4()), "workspace_id": ws_id, "user_id": uid, "content": "A written post.",
        "created_at": datetime.now(timezone.utc), "deleted": False,
    })
    words = [{"word": "spoken", "start_s": 0, "end_s": 1}, {"word": "words", "start_s": 1, "end_s": 2}]
    await _seed(audio_assets, ws_id, uid, id="a-upload", script=None, transcript=words)          # an upload
    await _seed(audio_assets, ws_id, uid, id="a-tts", script="Scripted narration.", transcript=words)
    await _seed(image_assets, ws_id, uid, id="i-upload", prompt=None, alt_text="A red bicycle by a wall.")
    await _seed(image_assets, ws_id, uid, id="i-ai", prompt="A calm sunrise", alt_text="ignored")
    await _seed(media_assets, ws_id, uid, id="v-done", kind="video", analysis_status="done", transcript=words)
    await _seed(media_assets, ws_id, uid, id="v-new", kind="video", analysis_status="none", transcript=[])
    await _seed(media_assets, ws_id, uid, id="img-media", kind="image", analysis_status="none")

    def texts(rows):
        return {r["id"]: r["text"] for r in rows}

    audio = texts(await iter_member_content(ws_id, uid, pipeline_type="audio"))
    assert audio["a-upload"] == "spoken words"                 # falls back to the transcript
    assert audio["a-tts"] == "Scripted narration."             # the script still wins when there is one

    image = texts(await iter_member_content(ws_id, uid, pipeline_type="image"))
    assert image["i-upload"] == "A red bicycle by a wall."     # falls back to the description
    assert image["i-ai"] == "A calm sunrise"                   # the prompt still wins

    video = texts(await iter_member_content(ws_id, uid, pipeline_type="video"))
    assert video == {"v-done": "spoken words"}                 # only an analysed video is readable content

    everything = await iter_member_content(ws_id, uid)
    assert {r["pipeline_type"] for r in everything} == {"text", "audio", "image", "video"}
