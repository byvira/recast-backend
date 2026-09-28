"""Tests for /api/v1/audio-assets — the real Audio pipeline (script TTS,
multi-voice dialogue, localization, upload with transcription + DSP,
export, governance). Previously ~10 endpoints with no automated regression
coverage.

Real Mongo, real audio: fixtures generate genuine WAV bytes, so the real DSP
(noisereduce + pyloudnorm) and the real dialogue stitching actually run. Only
the external effects are stubbed — TTS provider, Cloudinary upload, Groq
Whisper transcription, and the translation LLM.
"""

import io
from uuid import uuid4

import numpy as np
import pytest
import soundfile as sf

from app.api.v1 import audio_assets as audio_module
from app.db.mongo import (
    audio_asset_versions,
    audio_assets,
    guest_voice_profiles,
    media_assets,
    member_lexicon,
)
from app.models.audio_asset import TranscriptWord
from app.pipelines.media import tts_generation
from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from tests.conftest import create_workspace, invite_and_accept

_CLOUDINARY_URL = "https://res.cloudinary.com/demo/video/upload/v1/{name}.mp3"
_SR = 16000


def _wav(seconds: float = 0.5, noisy: bool = False) -> bytes:
    t = np.linspace(0, seconds, int(_SR * seconds), endpoint=False)
    signal = 0.3 * np.sin(2 * np.pi * 220 * t)
    if noisy:
        signal = signal + 0.02 * np.random.default_rng(1).standard_normal(signal.shape)
    buf = io.BytesIO()
    sf.write(buf, signal.astype("float32"), _SR, format="WAV")
    return buf.getvalue()


@pytest.fixture
def stubs(monkeypatch):
    """TTS, upload and transcription stubs. `record` captures every call."""
    record = {"synth": [], "uploads": [], "tts_returns_none": False}

    async def _fake_synth(*, text, voice_settings, lexicon=None, workspace_id, user_id):
        record["synth"].append({"text": text, "voice": voice_settings.tts_voice, "lexicon": lexicon})
        return None if record["tts_returns_none"] else _wav(0.4)

    async def _fake_upload(data, content_type, user_id):
        record["uploads"].append(data)
        return _CLOUDINARY_URL.format(name=uuid4().hex)

    async def _fake_transcribe(data, *, filename):
        return [TranscriptWord(word="hello", start_s=0.0, end_s=0.3),
                TranscriptWord(word="world", start_s=0.3, end_s=0.6)]

    monkeypatch.setattr(audio_module, "synthesize_speech", _fake_synth)
    monkeypatch.setattr(audio_module, "upload_file", _fake_upload)
    monkeypatch.setattr(audio_module, "transcribe_audio_bytes", _fake_transcribe)
    return record


@pytest.fixture
def translation(monkeypatch):
    """Scripted translation LLM: each call to call_llm pops the next reply
    (translate, score, translate, score, ...). Also makes any language
    'supported' so endpoint tests don't depend on which provider keys the
    dev .env happens to hold."""
    replies: list[str] = []

    async def _fake_llm(*args, **kwargs):
        return replies.pop(0)

    monkeypatch.setattr(audio_module, "call_llm", _fake_llm)
    monkeypatch.setattr(audio_module, "is_language_supported", lambda language: (True, ""))
    return replies


async def _brand(client, ws_id: str) -> str:
    res = await client.post(
        "/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code in (200, 201), res.text
    return res.json()["brand_profile_id"]


async def _setup(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Audio WS")
    return client, profile, ws_id, await _brand(client, ws_id)


def _h(ws_id: str) -> dict:
    return {"X-Workspace-Id": ws_id}


async def _generate(client, ws_id: str, brand_id: str, **overrides):
    body = {"title": "Episode 1", "brand_id": brand_id, "script": "Welcome to the show."}
    body.update(overrides)
    return await client.post("/api/v1/audio-assets/generate", json=body, headers=_h(ws_id))


# ── script -> TTS ────────────────────────────────────────────────────────────

async def test_generate_persists_a_real_synthesized_asset(signup_user, stubs):
    client, profile, ws_id, brand_id = await _setup(signup_user)

    res = await _generate(client, ws_id, brand_id)
    assert res.status_code == 201, res.text
    asset = res.json()

    assert asset["source_type"] == "script_tts"
    assert asset["script"] == "Welcome to the show."
    assert asset["approval_status"] == "pending"
    assert asset["created_by"] == profile["id"]
    assert asset["voice_settings_snapshot"]["speech_speed"] == 1.0

    media = await media_assets.find_one({"id": asset["media_id"]})
    assert media["kind"] == "audio"
    assert media["source"] == "synthesized"
    assert len(stubs["uploads"]) == 1

    # A version history exists from the start: v1 = as first created.
    versions = (await client.get(f"/api/v1/audio-assets/{asset['id']}/versions", headers=_h(ws_id))).json()
    assert versions["total"] == 1
    assert versions["versions"][0]["version_number"] == 1
    assert versions["versions"][0]["media_id"] == asset["media_id"]


async def test_generate_validation_and_provider_failure_create_nothing(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)

    assert (await _generate(client, ws_id, brand_id, script=None)).status_code == 400
    assert (await _generate(client, ws_id, brand_id, script=None, source_piece_id="nope")).status_code == 404

    stubs["tts_returns_none"] = True
    res = await _generate(client, ws_id, brand_id)
    assert res.status_code == 503, res.text

    assert stubs["uploads"] == []
    assert await audio_assets.count_documents({"workspace_id": ws_id}) == 0
    assert await media_assets.count_documents({"workspace_id": ws_id}) == 0


async def test_generate_from_a_source_piece_uses_its_content_as_the_script(signup_user, stubs):
    client, profile, ws_id, brand_id = await _setup(signup_user)
    session_id = str(uuid4())
    await ensure_session_exists(
        session_id=session_id, workspace_id=ws_id, user_id=profile["id"],
        brand_id=brand_id, source_type="text",
    )
    piece_id = await save_live_piece(
        session_id=session_id, workspace_id=ws_id, user_id=profile["id"], brand_id=brand_id,
        platform="LinkedIn", content="  A post worth hearing aloud.  ", word_count=6, char_count=30,
    )

    res = await _generate(client, ws_id, brand_id, script=None, source_piece_id=piece_id)
    assert res.status_code == 201, res.text
    assert res.json()["script"] == "A post worth hearing aloud."
    assert res.json()["source_piece_id"] == piece_id
    assert res.json()["source_content_hash"]


async def test_the_members_lexicon_reaches_synthesis(signup_user, stubs):
    from datetime import datetime, timezone

    client, profile, ws_id, brand_id = await _setup(signup_user)
    now = datetime.now(timezone.utc)
    await member_lexicon.insert_one({
        "id": f"{ws_id}:{profile['id']}", "workspace_id": ws_id, "user_id": profile["id"],
        "pronunciations": [{"id": "1", "term": "Zendly", "ipa": "zen-dlee", "notes": ""}],
        "whitelist": [], "blacklist": [], "writing_blueprint": {}, "created_at": now, "updated_at": now,
    })

    res = await _generate(client, ws_id, brand_id)
    assert res.status_code == 201, res.text

    lexicon = stubs["synth"][-1]["lexicon"]
    assert lexicon is not None
    assert [p.term for p in lexicon.pronunciations] == ["Zendly"]


async def test_viewers_cannot_generate_audio(signup_user, make_client, stubs):
    owner, _, ws_id, brand_id = await _setup(signup_user)
    viewer, _ = await invite_and_accept(owner, make_client, ws_id, "viewer")

    assert (await _generate(viewer, ws_id, brand_id)).status_code == 403
    assert stubs["synth"] == []


# ── multi-voice dialogue ─────────────────────────────────────────────────────

async def test_dialogue_stitches_real_audio_and_records_guest_voices(signup_user, stubs):
    from datetime import datetime, timezone

    client, profile, ws_id, brand_id = await _setup(signup_user)
    now = datetime.now(timezone.utc)
    await member_lexicon.insert_one({
        "id": f"{ws_id}:{profile['id']}", "workspace_id": ws_id, "user_id": profile["id"],
        "pronunciations": [{"id": "1", "term": "Zendly", "ipa": "zen-dlee", "notes": ""}],
        "whitelist": [], "blacklist": [], "writing_blueprint": {}, "created_at": now, "updated_at": now,
    })

    res = await client.post(
        "/api/v1/audio-assets/dialogue",
        json={
            "title": "Interview", "brand_id": brand_id,
            "turns": [
                {"speaker": "Host", "text": "Welcome to Zendly."},
                {"speaker": "Guest", "text": "Glad to be here.", "voice_id": "aura-2-luna-en"},
                {"speaker": "Host", "text": "Let's begin."},
            ],
        },
        headers=_h(ws_id),
    )
    assert res.status_code == 201, res.text
    asset = res.json()
    assert asset["source_type"] == "dialogue"
    assert asset["script"] == "Host: Welcome to Zendly.\nGuest: Glad to be here.\nHost: Let's begin."

    # The uploaded file is one real continuous WAV of all three 0.4s turns.
    data, sr = sf.read(io.BytesIO(stubs["uploads"][-1]))
    assert sr == _SR
    assert len(data) / sr == pytest.approx(1.2, abs=0.05)

    # The member's pronunciations apply to their own turns only, never a guest's.
    by_text = {c["text"]: c for c in stubs["synth"]}
    assert by_text["Welcome to Zendly."]["lexicon"] is not None
    assert by_text["Glad to be here."]["lexicon"] is None
    assert by_text["Glad to be here."]["voice"] == "aura-2-luna-en"

    guests = await guest_voice_profiles.find({"audio_asset_id": asset["id"]}).to_list(10)
    assert [(g["name"], g["voice_id"]) for g in guests] == [("Guest", "aura-2-luna-en")]


async def test_dialogue_rejects_empty_turns_and_aborts_on_a_failed_turn(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    url = "/api/v1/audio-assets/dialogue"

    empty = await client.post(url, json={"title": "t", "brand_id": brand_id, "turns": []}, headers=_h(ws_id))
    assert empty.status_code == 400

    stubs["tts_returns_none"] = True
    failed = await client.post(
        url, json={"title": "t", "brand_id": brand_id, "turns": [{"speaker": "A", "text": "Hi."}]},
        headers=_h(ws_id),
    )
    assert failed.status_code == 503
    assert await audio_assets.count_documents({"workspace_id": ws_id}) == 0


# ── upload (real DSP + transcription) ────────────────────────────────────────

async def _upload(client, ws_id, brand_id, data: bytes, content_type="audio/wav", name="take.wav"):
    return await client.post(
        "/api/v1/audio-assets/upload",
        data={"title": "Raw take", "brand_id": brand_id},
        files={"file": (name, data, content_type)},
        headers=_h(ws_id),
    )


async def test_upload_runs_real_dsp_and_stores_the_transcript(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)

    res = await _upload(client, ws_id, brand_id, _wav(1.0, noisy=True))
    assert res.status_code == 201, res.text
    asset = res.json()

    assert asset["source_type"] == "uploaded"
    assert asset["dsp_settings"], "real denoise + loudness-normalize must have run"
    assert [w["word"] for w in asset["transcript"]] == ["hello", "world"]

    media = await media_assets.find_one({"id": asset["media_id"]})
    assert media["source"] == "enhanced"
    assert media["mime_type"] == "audio/wav"
    # What got uploaded is the processed audio, not the untouched original.
    assert stubs["uploads"][-1] != _wav(1.0, noisy=True)
    processed, sr = sf.read(io.BytesIO(stubs["uploads"][-1]))
    assert len(processed) > 0 and sr > 0


async def test_an_uploaded_recordings_words_reach_the_agents(signup_user, stubs, monkeypatch):
    """The event Odette reads used to carry content_text="" for an upload, so
    nothing a member uploaded could ever be read. It now carries the words."""
    from app.agents.personal.history import iter_member_content

    events: list = []
    monkeypatch.setattr(audio_module, "emit_event_background", lambda **kw: events.append(kw))
    client, profile, ws_id, brand_id = await _setup(signup_user)

    res = await _upload(client, ws_id, brand_id, _wav(0.5))
    assert res.status_code == 201, res.text

    assert events[-1]["payload"].content_text == "hello world"
    assert events[-1]["payload"].content_summary == "hello world"

    # And Remy's history reads the transcript, since an upload has no script.
    rows = await iter_member_content(ws_id, profile["id"], pipeline_type="audio")
    assert [r["text"] for r in rows] == ["hello world"]


async def test_a_dsp_failure_never_blocks_the_upload(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)

    def _boom(*args, **kwargs):
        raise RuntimeError("dsp exploded")

    monkeypatch.setattr(audio_module, "enhance_audio", _boom)
    original = _wav(0.5)

    res = await _upload(client, ws_id, brand_id, original)
    assert res.status_code == 201, res.text
    assert res.json()["dsp_settings"] == {}
    media = await media_assets.find_one({"id": res.json()["media_id"]})
    assert media["source"] == "uploaded"
    assert stubs["uploads"][-1] == original  # the original recording, untouched


async def test_upload_rejects_unsupported_types(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)

    res = await _upload(client, ws_id, brand_id, b"not audio", content_type="text/plain", name="x.txt")
    assert res.status_code == 400
    assert stubs["uploads"] == []


# ── localization ─────────────────────────────────────────────────────────────

async def test_localize_translates_resynthesizes_and_links_back(signup_user, stubs, translation):
    client, _, ws_id, brand_id = await _setup(signup_user)
    source = (await _generate(client, ws_id, brand_id, script="Good morning everyone.")).json()
    translation.extend(["Bonjour tout le monde.", "SCORE: 0.92 REASON: natural"])

    res = await client.post(
        f"/api/v1/audio-assets/{source['id']}/localize", json={"target_language": "French"},
        headers=_h(ws_id),
    )
    assert res.status_code == 201, res.text
    localized = res.json()

    assert localized["language"] == "French"
    assert localized["script"] == "Bonjour tout le monde."
    assert localized["source_audio_asset_id"] == source["id"]
    assert localized["title"] == "Episode 1 (French)"
    assert localized["approval_status"] == "pending"  # its own approval gate
    assert stubs["synth"][-1]["text"] == "Bonjour tout le monde."

    versions = (await client.get(f"/api/v1/audio-assets/{localized['id']}/versions", headers=_h(ws_id))).json()
    assert versions["total"] == 1


async def test_localize_falls_back_to_the_transcript_for_uploaded_audio(signup_user, stubs, translation):
    client, _, ws_id, brand_id = await _setup(signup_user)
    uploaded = (await _upload(client, ws_id, brand_id, _wav(0.5))).json()
    translation.extend(["Bonjour le monde", "SCORE: 0.9 REASON: ok"])

    res = await client.post(
        f"/api/v1/audio-assets/{uploaded['id']}/localize", json={"target_language": "French"},
        headers=_h(ws_id),
    )
    assert res.status_code == 201, res.text
    assert res.json()["script"] == "Bonjour le monde"


async def test_localize_errors(signup_user, stubs, translation, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    source = (await _generate(client, ws_id, brand_id)).json()
    url = f"/api/v1/audio-assets/{source['id']}/localize"

    missing = await client.post(
        "/api/v1/audio-assets/nope/localize", json={"target_language": "French"}, headers=_h(ws_id),
    )
    assert missing.status_code == 404

    # No script and no transcript to translate.
    await audio_assets.update_one({"id": source["id"]}, {"$set": {"script": None, "transcript": []}})
    empty = await client.post(url, json={"target_language": "French"}, headers=_h(ws_id))
    assert empty.status_code == 400

    # A language no reachable provider can voice fails BEFORE any translation spend.
    monkeypatch.setattr(audio_module, "is_language_supported", lambda language: (False, "not voiceable"))
    unsupported = await client.post(url, json={"target_language": "Klingon"}, headers=_h(ws_id))
    assert unsupported.status_code == 400
    assert "not voiceable" in unsupported.json()["detail"]
    assert translation == []  # the LLM was never called


async def test_translation_gate_retries_and_keeps_the_best_attempt(monkeypatch):
    replies = ["weak", "SCORE: 0.3 REASON: literal", "strong", "SCORE: 0.9 REASON: natural"]

    async def _fake_llm(*args, **kwargs):
        return replies.pop(0)

    monkeypatch.setattr(audio_module, "call_llm", _fake_llm)
    text, score = await audio_module._translate_with_quality_gate("source", "French")
    assert (text, score) == ("strong", 0.9)
    assert replies == []


async def test_translation_gate_gives_up_after_three_attempts_keeping_the_highest(monkeypatch):
    replies = [
        "a", "SCORE: 0.2 REASON: bad", "b", "SCORE: 0.6 REASON: meh", "c", "SCORE: 0.4 REASON: worse",
    ]

    async def _fake_llm(*args, **kwargs):
        return replies.pop(0)

    monkeypatch.setattr(audio_module, "call_llm", _fake_llm)
    text, score = await audio_module._translate_with_quality_gate("source", "French")
    assert (text, score) == ("b", 0.6)  # best of three, not the last


def test_a_malformed_score_counts_as_failing_instead_of_raising():
    score, reason = audio_module._parse_translation_score("no score here at all")
    assert score == 0.0 and reason

    assert audio_module._parse_translation_score("SCORE: 7 REASON: x")[0] == 1.0  # clamped
    assert audio_module._parse_translation_score("SCORE: 0.85 REASON: fine")[0] == 0.85


def test_language_support_reflects_which_providers_are_reachable(monkeypatch):
    """The live-confirmed gap: Deepgram's Aura has no Tamil, ElevenLabs does."""
    monkeypatch.setattr(tts_generation.settings, "ELEVENLABS_API_KEY", "")
    monkeypatch.setattr(tts_generation.settings, "DEEPGRAM_API_KEY", "dg")
    ok, _ = tts_generation.is_language_supported("english")
    assert ok
    ok, reason = tts_generation.is_language_supported("tamil")
    assert not ok and "Deepgram" in reason

    monkeypatch.setattr(tts_generation.settings, "ELEVENLABS_API_KEY", "el")
    assert tts_generation.is_language_supported("tamil")[0] is True

    monkeypatch.setattr(tts_generation.settings, "ELEVENLABS_API_KEY", "")
    monkeypatch.setattr(tts_generation.settings, "DEEPGRAM_API_KEY", "")
    ok, reason = tts_generation.is_language_supported("english")
    assert not ok and "No TTS provider" in reason


# ── export ───────────────────────────────────────────────────────────────────

async def test_export_creates_a_derivative_and_rejects_unknown_formats(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    url = f"/api/v1/audio-assets/{asset['id']}/export"

    res = await client.post(url, json={"export_format": "wav"}, headers=_h(ws_id))
    assert res.status_code == 201, res.text
    assert res.json()["mime_type"] == "audio/wav"
    assert res.json()["source"] == "edited"
    assert res.json()["id"] != asset["media_id"]

    assert (await client.post(url, json={"export_format": "flac"}, headers=_h(ws_id))).status_code == 400
    missing = await client.post(
        "/api/v1/audio-assets/nope/export", json={"export_format": "mp3"}, headers=_h(ws_id),
    )
    assert missing.status_code == 404


# ── governance ───────────────────────────────────────────────────────────────

async def test_approve_pins_the_master_file_and_reject_overrides(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    base = f"/api/v1/audio-assets/{asset['id']}"

    res = await client.patch(f"{base}/approve", headers=_h(ws_id))
    assert res.status_code == 200, res.text
    assert res.json()["approval_status"] == "approved"
    assert res.json()["approved_master_media_id"] == asset["media_id"]

    assert (await client.patch(f"{base}/reject", headers=_h(ws_id))).json()["approval_status"] == "rejected"


async def test_editors_cannot_approve(signup_user, make_client, stubs):
    owner, _, ws_id, brand_id = await _setup(signup_user)
    editor, _ = await invite_and_accept(owner, make_client, ws_id, "editor")
    asset = (await _generate(owner, ws_id, brand_id)).json()

    res = await editor.patch(f"/api/v1/audio-assets/{asset['id']}/approve", headers=_h(ws_id))
    assert res.status_code == 403


async def test_restore_brings_back_a_versions_file_as_a_new_version(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    base = f"/api/v1/audio-assets/{asset['id']}"

    # Simulate the active file having moved on (a later re-take/replace),
    # then restore the original: the ORIGINAL file must come back.
    await audio_assets.update_one({"id": asset["id"]}, {"$set": {"media_id": "some-other-media"}})

    res = await client.post(f"{base}/restore/1", headers=_h(ws_id))
    assert res.status_code == 200, res.text
    assert res.json()["media_id"] == asset["media_id"]
    assert res.json()["version_count"] == 2  # a restore is itself a new version, never a rewind

    versions = (await client.get(f"{base}/versions", headers=_h(ws_id))).json()
    assert [v["version_number"] for v in versions["versions"]] == [1, 2]
    assert versions["versions"][1]["action"] == "restored_from_v1"

    assert (await client.post(f"{base}/restore/99", headers=_h(ws_id))).status_code == 404


async def test_restore_refuses_a_version_with_no_file_recorded(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    await audio_asset_versions.update_one(
        {"audio_asset_id": asset["id"], "version_number": 1}, {"$set": {"media_id": None}},
    )

    res = await client.post(f"/api/v1/audio-assets/{asset['id']}/restore/1", headers=_h(ws_id))
    assert res.status_code == 400


async def test_share_link_has_a_token_url_and_expiry(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()

    res = await client.post(f"/api/v1/audio-assets/{asset['id']}/share-link", headers=_h(ws_id))
    assert res.status_code == 201, res.text
    body = res.json()
    assert len(body["token"]) >= 24
    assert body["url"].endswith(f"/share/{body['token']}")
    assert body["expires_at"]


async def test_assets_are_invisible_across_workspaces(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    other_ws = await create_workspace(client, "Other Audio WS")

    for method, path in (
        ("patch", f"/api/v1/audio-assets/{asset['id']}/approve"),
        ("get", f"/api/v1/audio-assets/{asset['id']}/versions"),
        ("post", f"/api/v1/audio-assets/{asset['id']}/share-link"),
        ("post", f"/api/v1/audio-assets/{asset['id']}/restore/1"),
    ):
        res = await getattr(client, method)(path, headers=_h(other_ws))
        assert res.status_code == 404, (method, path)
