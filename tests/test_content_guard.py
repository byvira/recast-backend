"""Content Guard: the cleanup and screening rules, the agent's verdicts, the shared model wrapper, the saved piece, the
publish gate and the member and Ops routes. No model is called: rewrites and checks are stubbed."""

from unittest.mock import AsyncMock, patch

import pytest

from app.agents.content_guard import config as guard_config
from app.agents.content_guard.agent import _model_verdicts, check_piece_before_send, guard_piece_doc, review_text, rule_screen
from app.agents.content_guard.rules import ai_phrases_in, clean_text, clean_value, risk_signals, screen_text, strip_dashes
from app.core.config import settings
from app.db.mongo import content_pieces, content_safety, safety_events, users
from app.pipelines.publish.spine import check_gate, schedule_blocker, unsafe_reason
from app.shared import llm as llm_module
from tests.conftest import create_workspace, signup_new_user
from tests.test_publish_spine import H, _approve, _connect, _seed

DASH = "—"


@pytest.fixture(autouse=True)
async def clean_state(monkeypatch):
    monkeypatch.setattr(settings, "CONTENT_GUARD_LIVE_CHECKS", True)
    await content_safety.delete_many({})
    await safety_events.delete_many({})
    guard_config.reset_cache()
    _model_verdicts.clear()
    yield
    await content_safety.delete_many({})
    await safety_events.delete_many({})
    guard_config.reset_cache()


# ── cleanup ───────────────────────────────────────────────────────────────────

def test_em_dashes_become_commas_and_keep_the_word_boundary():
    assert strip_dashes(f"Great {DASH} and useful") == "Great, and useful"
    assert strip_dashes(f"Great{DASH}impactful") == "Great, impactful"
    assert strip_dashes(f"Wait {DASH}. Done") == "Wait. Done"


def test_spaced_en_dashes_go_but_ranges_stay():
    assert strip_dashes("Open – closed") == "Open, closed"
    assert strip_dashes("Open 9–5 daily") == "Open 9–5 daily"


def test_invisible_characters_and_chat_wrapping_are_removed():
    assert clean_text("Hel​lo wor­ld") == "Hello world"
    assert clean_text("Sure! Here is your post:\n\nLaunch day is here.") == "Launch day is here."
    assert clean_text("Launch day is here.\n\nLet me know if you want changes!") == "Launch day is here."


def test_a_real_opening_line_is_not_mistaken_for_a_chat_preamble():
    text = "Here's what I learned after 5 years:\n\n1. Start small."
    assert clean_text(text) == text


def test_filler_wording_is_swapped_for_plain_wording():
    out = clean_text("In today's fast-paced world, we delve into pricing.")
    assert "fast-paced" not in out and "dig into pricing" in out


def test_clean_value_reaches_into_dicts_and_lists():
    assert clean_value({"a": [f"x {DASH} y", 3], "b": {"c": f"p{DASH}q"}}) == {"a": ["x, y", 3], "b": {"c": "p, q"}}


def test_listed_machine_sounding_phrases_are_reported_not_rewritten():
    assert ai_phrases_in("It’s important to note this is a game-changer.") == ["it's important to note", "game-changer"]


# ── screening ─────────────────────────────────────────────────────────────────

def test_ordinary_marketing_text_passes():
    assert screen_text("We shipped a faster dashboard. Try it free and tell us what you think.").ok


@pytest.mark.parametrize("text,category", [
    ("Watch our new porn collection", "sexual"),
    ("All of them should die", "hate"),
    ("I will kill you", "violence"),
    ("You should just kill yourself", "self_harm"),
    ("As an AI language model, I cannot write that.", "assistant_leak"),
    ("Hi [insert your name here]", "assistant_leak"),
])
def test_unsafe_text_is_flagged_with_its_reason(text, category):
    result = screen_text(text)
    assert not result.ok and category in result.categories and result.message()


def test_strict_adds_strong_language_and_milder_adult_words():
    assert screen_text("This is bullshit and a nude shade").ok
    strict = screen_text("This is bullshit and a nude shade", strictness="strict")
    assert not strict.ok and {"profanity", "sexual"} <= set(strict.categories)


def test_terms_ops_allows_are_never_counted():
    assert not screen_text("Our nude lipstick", strictness="strict").ok
    assert screen_text("Our nude lipstick", strictness="strict", allowed_terms=["nude lipstick"]).ok


def test_ops_own_blocked_terms_are_matched_as_whole_words():
    assert not screen_text("buy crypto-scam today", extra_blocked_terms=["crypto-scam"]).ok
    assert screen_text("scampi tonight", extra_blocked_terms=["scam"]).ok


# ── the agent ─────────────────────────────────────────────────────────────────

async def test_clean_text_passes_untouched_and_costs_no_model_call():
    with patch("app.shared.llm.call_llm", new=AsyncMock()) as model:
        result = await review_text("A calm post about our roadmap.")
    assert result.outcome == "clean" and result.text == "A calm post about our roadmap." and not model.called


async def test_dashes_alone_give_a_fixed_verdict():
    result = await review_text(f"Fast {DASH} and simple")
    assert result.outcome == "fixed" and result.text == "Fast, and simple"


async def test_a_flagged_text_is_rewritten_and_the_catch_is_recorded():
    with patch("app.shared.llm.call_llm", new=AsyncMock(return_value="Come and see our new collection.")):
        result = await review_text("Come and see our new porn collection.", where="check", workspace_id="w1", piece_id="p1")
    assert result.outcome == "rewritten" and result.text == "Come and see our new collection."
    rows = [r async for r in safety_events.find({})]
    assert len(rows) == 1 and rows[0]["outcome"] == "rewritten" and rows[0]["categories"] == ["sexual"]


async def test_a_rewrite_that_is_still_unsafe_ends_blocked():
    with patch("app.shared.llm.call_llm", new=AsyncMock(return_value="More porn inside.")):
        result = await review_text("Some porn here.")
    assert result.blocked and result.message and result.categories == ["sexual"]
    assert (await safety_events.find_one({}))["outcome"] == "blocked"


async def test_with_rewriting_off_a_flagged_text_is_blocked_without_a_model_call():
    await guard_config.save({"rewrite_flagged": False}, expected_version=0, user_id="u")
    with patch("app.shared.llm.call_llm", new=AsyncMock()) as model:
        result = await review_text("Some porn here.")
    assert result.blocked and not model.called


async def test_a_failing_rewrite_blocks_instead_of_raising():
    with patch("app.shared.llm.call_llm", new=AsyncMock(side_effect=RuntimeError("down"))):
        assert (await review_text("Some porn here.")).blocked


async def test_the_same_blocked_text_is_stored_once():
    await guard_config.save({"rewrite_flagged": False}, expected_version=0, user_id="u")
    for _ in range(3):
        await review_text("Some porn here.", workspace_id="w1", piece_id="p1")
    assert await safety_events.count_documents({}) == 1


async def test_turned_off_the_guard_still_cleans_dashes_but_does_not_screen():
    await guard_config.save({"enabled": False}, expected_version=0, user_id="u")
    result = await review_text(f"Some porn {DASH} here.")
    assert result.outcome == "fixed" and not rule_screen("Some porn here.").categories


async def test_the_model_check_can_block_what_the_rules_miss():
    await guard_config.save({"model_check": "publish", "rewrite_flagged": False}, expected_version=0, user_id="u")
    reply = {"unsafe": True, "categories": ["hate"], "reason": "dehumanising"}
    with patch("app.shared.llm.call_llm_structured", new=AsyncMock(return_value=reply)) as model:
        result = await review_text("A subtle but cruel post.", stage="publish")
        again = await review_text("A subtle but cruel post.", stage="publish")
    assert result.blocked and result.categories == ["hate"] and again.blocked
    assert model.call_count == 1, "the second look uses the remembered verdict"


async def test_at_generation_the_model_only_reads_texts_with_risk_signals():
    await guard_config.save({"model_check": "publish"}, expected_version=0, user_id="u")
    ok = {"unsafe": False, "categories": [], "reason": ""}
    with patch("app.shared.llm.call_llm_structured", new=AsyncMock(return_value=ok)) as model:
        await review_text("A calm post about our roadmap.")
        assert not model.called
        await review_text("A post that talks about abuse prevention.")
        assert model.call_count == 1


async def test_with_the_model_check_off_nothing_is_sent_to_a_model():
    await guard_config.save({"model_check": "off"}, expected_version=0, user_id="u")
    with patch("app.shared.llm.call_llm_structured", new=AsyncMock()) as model:
        await review_text("A post about abuse prevention.", stage="publish")
    assert not model.called


# ── disguises and the maintained word list ────────────────────────────────────

@pytest.mark.parametrize("text", ["buy p0rn now", "buy p o r n now", "buy porrrn now", "buy p*rn now".replace("*", "o"), "buy pоrn now"])
def test_common_disguises_are_read_as_the_word(text):
    assert not screen_text(text).ok


def test_strict_uses_the_maintained_word_list_and_sees_through_disguises():
    assert screen_text("what the fuuuck is this").ok is True
    for text in ("what the fuuuck is this", "what the f u c k is this", "what the f.u.c.k is this"):
        assert not screen_text(text, strictness="strict").ok, text
    assert screen_text("A calm product update with three new features.", strictness="strict").ok


def test_the_word_list_respects_terms_ops_allows():
    assert screen_text("what the fuck", strictness="strict", allowed_terms=["fuck"]).ok


def test_risk_signals_point_a_model_at_texts_worth_a_second_read():
    assert risk_signals("A calm roadmap update.") == []
    assert any("topic" in s for s in risk_signals("This post discusses abuse."))
    assert any("word" in s for s in risk_signals("what the fuck"))


# ── one model reading before a post goes out ──────────────────────────────────

async def test_a_post_is_read_once_before_sending_and_the_verdict_stays_with_the_post(make_client):
    client = make_client()
    user = await signup_new_user(client)
    ws = await create_workspace(client, "Read Once WS")
    piece_id = await _seed(ws, user["id"])
    piece = await content_pieces.find_one({"piece_id": piece_id})
    bad = {"unsafe": True, "categories": ["hate"], "reason": "cruel"}
    with patch("app.shared.llm.call_llm_structured", new=AsyncMock(return_value=bad)) as model:
        await check_piece_before_send(piece, ws)
        await check_piece_before_send(piece, ws)
    assert model.call_count == 1
    stored = (await content_pieces.find_one({"piece_id": piece_id}))["safety_check"]
    assert stored["ok"] is False and stored["categories"] == ["hate"]
    held = dict(piece)
    held["safety_check"] = stored
    block = check_gate({**held, "approval_status": "approved"}, confirm_anyway=True)
    assert block and block.code == "UNSAFE_CONTENT"
    assert (await safety_events.find_one({"where": "publish"}))["outcome"] == "blocked"

    # Edited text is a different text: the old verdict no longer applies.
    edited = {**held, "content": "A new, kind post.", "approval_status": "approved"}
    assert check_gate(edited) is None


async def test_when_the_model_check_cannot_run_the_post_is_judged_by_the_rules(make_client):
    client = make_client()
    user = await signup_new_user(client)
    ws = await create_workspace(client, "Offline WS")
    piece = await content_pieces.find_one({"piece_id": await _seed(ws, user["id"])})
    with patch("app.shared.llm.call_llm_structured", new=AsyncMock(side_effect=RuntimeError("down"))):
        await check_piece_before_send(piece, ws)
    assert "safety_check" not in piece
    assert check_gate({**piece, "approval_status": "approved"}) is None


# ── the shared model wrapper ──────────────────────────────────────────────────

async def test_every_model_reply_is_cleaned_on_the_way_out():
    async def text_reply():
        return f"Fast {DASH} simple"

    async def structured_reply():
        return {"post": f"a{DASH}b", "n": 2}

    assert await llm_module._tidy_output(text_reply)() == "Fast, simple"
    assert await llm_module._tidy_output(structured_reply)() == {"post": "a, b", "n": 2}


def test_the_public_model_calls_are_wrapped():
    for name in ("call_llm", "call_llm_structured", "call_llm_chat", "call_llm_fallback"):
        assert hasattr(getattr(llm_module, name), "__wrapped__"), name


# ── a piece being saved ───────────────────────────────────────────────────────

async def test_a_saved_piece_loses_its_dashes_and_unsafe_text_flags_it():
    doc = {"content": f"Fast {DASH} simple", "sections": [{"content": f"a{DASH}b"}], "hooks": [{"text": f"x{DASH}y"}], "workspace_id": "w1", "piece_id": "p1"}
    await guard_piece_doc(doc, where="generation")
    assert doc["content"] == "Fast, simple" and doc["sections"][0]["content"] == "a, b" and doc["hooks"][0]["text"] == "x, y"
    assert not doc.get("flagged_for_review")

    await guard_config.save({"rewrite_flagged": False}, expected_version=0, user_id="u")
    unsafe = {"content": "Hot porn inside", "quality_passed": True, "quality_issues": [], "workspace_id": "w1", "piece_id": "p2"}
    await guard_piece_doc(unsafe, where="generation")
    assert unsafe["flagged_for_review"] is True and unsafe["quality_passed"] is False
    assert any("sexual" in issue.lower() for issue in unsafe["quality_issues"])


# ── the publish gate ──────────────────────────────────────────────────────────

def test_the_gate_refuses_unsafe_text_even_when_the_person_chooses_publish_anyway():
    piece = {"content": "Hot porn inside", "approval_status": "approved"}
    block = check_gate(piece, confirm_anyway=True)
    assert block and block.code == "UNSAFE_CONTENT"
    assert unsafe_reason({"content": "A calm post."}) is None


def test_a_clean_approved_piece_still_goes_through():
    assert check_gate({"content": "A calm post.", "approval_status": "approved"}) is None


async def test_scheduling_and_publishing_unsafe_posts_are_refused(make_client):
    client = make_client()
    user = await signup_new_user(client)
    ws = await create_workspace(client, "Guard WS")
    await _connect(ws)
    piece_id = await _seed(ws, user["id"])
    await _approve(client, ws, piece_id)
    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {"content": "Hot porn inside"}})
    piece = await content_pieces.find_one({"piece_id": piece_id})
    blocker = await schedule_blocker(piece, ws)
    assert blocker and blocker[0] == 409
    res = await client.post("/api/v1/publish/now", headers=H(ws), json={"piece_id": piece_id, "confirm_publish_anyway": True})
    assert res.status_code == 409 and res.json()["detail"]["code"] == "UNSAFE_CONTENT"


# ── routes ────────────────────────────────────────────────────────────────────

async def _staff(make_client, master: bool):
    client = make_client()
    user = await signup_new_user(client, name="Ops Person")
    fields = {"is_platform_staff": True}
    if master:
        fields["is_master_admin"] = True
    await users.update_one({"id": user["id"]}, {"$set": fields})
    return client, user


async def test_a_member_can_check_a_text(make_client):
    client = make_client()
    await signup_new_user(client)
    ws = await create_workspace(client, "Check WS")
    res = await client.post("/api/v1/content-guard/check", headers=H(ws), json={"text": f"Fast {DASH} simple"})
    assert res.status_code == 200 and res.json()["text"] == "Fast, simple" and res.json()["outcome"] == "fixed"
    with patch("app.shared.llm.call_llm", new=AsyncMock(return_value="Come and see it.")):
        res = await client.post("/api/v1/content-guard/check", headers=H(ws), json={"text": "Come see the porn"})
    assert res.json()["outcome"] == "rewritten"


async def test_staff_read_the_settings_and_only_the_owner_changes_them(make_client):
    staff, _ = await _staff(make_client, master=False)
    owner, _ = await _staff(make_client, master=True)
    base = "/api/v1/ops/content-safety"
    got = await staff.get(base)
    assert got.status_code == 200 and got.json()["strictness"] == "standard" and got.json()["version"] == 0
    assert (await staff.put(base, json={"version": 0, "strictness": "strict"})).status_code == 403

    saved = await owner.put(base, json={"version": 0, "strictness": "strict", "allowed_terms": ["Nude Lipstick", "nude lipstick"]})
    assert saved.status_code == 200 and saved.json()["version"] == 1 and saved.json()["allowed_terms"] == ["nude lipstick"]
    stale = await owner.put(base, json={"version": 0, "strictness": "standard"})
    assert stale.status_code == 409
    assert (await owner.put(base, json={"version": 1, "strictness": "loud"})).status_code == 422


async def test_ops_can_try_a_text_and_read_what_was_caught(make_client):
    staff, _ = await _staff(make_client, master=False)
    base = "/api/v1/ops/content-safety"
    tried = await staff.post(f"{base}/try", json={"text": f"Hot porn {DASH} inside. It's a game-changer."})
    body = tried.json()
    assert tried.status_code == 200 and body["ok"] is False and body["categories"] == ["sexual"]
    assert DASH not in body["cleaned"] and body["ai_phrases"] == ["game-changer"]

    await guard_config.save({"rewrite_flagged": False}, expected_version=0, user_id="u")
    await review_text("Some porn here.", where="check", workspace_id="w1", piece_id="p1")
    events = (await staff.get(f"{base}/events", params={"outcome": "blocked"})).json()
    assert events["total"] == 1 and events["events"][0]["category_names"] == ["Sexual or adult content"]
    assert (await staff.get(f"{base}/events", params={"outcome": "other"})).status_code == 422


async def test_members_cannot_read_the_ops_settings(make_client):
    client = make_client()
    await signup_new_user(client)
    assert (await client.get("/api/v1/ops/content-safety")).status_code == 403


# ── pictures, recordings and video ────────────────────────────────────────────

import json  # noqa: E402

from app.agents.content_guard import media as guard_media  # noqa: E402
from app.agents.content_guard.media import ContentRejected, assert_image_ok, assert_speech_ok, episode_is_safe, speech_problem  # noqa: E402

UNSAFE_PICTURE = json.dumps({"unsafe": True, "categories": ["sexual"], "reason": "explicit nudity"})
SAFE_PICTURE = json.dumps({"unsafe": False, "categories": [], "reason": ""})


@pytest.fixture(autouse=True)
def _forget_picture_verdicts():
    guard_media._VERDICTS.clear()
    yield
    guard_media._VERDICTS.clear()


async def test_an_unsafe_picture_is_refused_with_a_friendly_message_and_recorded():
    with patch("app.shared.llm.call_vision", new=AsyncMock(return_value=UNSAFE_PICTURE)):
        with pytest.raises(ContentRejected) as caught:
            await assert_image_ok(b"picture-bytes-1", "image/png", workspace_id="w1", where="upload")
    detail = caught.value.detail
    assert caught.value.status_code == 422 and detail["code"] == "CONTENT_REJECTED"
    assert "choose a different one" in detail["message"] and "sexual" in detail["message"].lower()
    assert (await safety_events.find_one({"where": "upload"}))["outcome"] == "blocked"


async def test_a_safe_picture_passes_and_is_read_only_once():
    with patch("app.shared.llm.call_vision", new=AsyncMock(return_value=SAFE_PICTURE)) as vision:
        await assert_image_ok(b"picture-bytes-2", "image/png", workspace_id="w1", where="upload")
        await assert_image_ok(b"picture-bytes-2", "image/png", workspace_id="w1", where="upload")
    assert vision.call_count == 1


async def test_a_picture_is_let_through_when_the_vision_model_is_unavailable():
    with patch("app.shared.llm.call_vision", new=AsyncMock(side_effect=RuntimeError("down"))):
        await assert_image_ok(b"picture-bytes-3", "image/png", workspace_id="w1", where="upload")


async def test_with_picture_checks_off_the_vision_model_is_never_asked():
    await guard_config.save({"media_check": False}, expected_version=0, user_id="u")
    with patch("app.shared.llm.call_vision", new=AsyncMock()) as vision:
        await assert_image_ok(b"picture-bytes-4", "image/png", workspace_id="w1", where="upload")
    assert not vision.called


async def test_uploading_an_unsafe_picture_returns_the_friendly_error(make_client):
    client = make_client()
    await signup_new_user(client)
    ws = await create_workspace(client, "Picture WS")
    with patch("app.shared.llm.call_vision", new=AsyncMock(return_value=UNSAFE_PICTURE)):
        res = await client.post("/api/v1/media", headers=H(ws), files={"file": ("a.png", b"\x89PNG-fake-bytes", "image/png")})
    assert res.status_code == 422 and res.json()["detail"]["code"] == "CONTENT_REJECTED"


async def test_unsafe_words_in_a_recording_or_script_are_refused_with_a_friendly_message():
    assert speech_problem("A calm talk about our roadmap.") is None
    message, categories = speech_problem("this recording has porn in it", "recording")
    assert categories == ["sexual"] and message.startswith("We can't use this recording")
    with pytest.raises(ContentRejected) as caught:
        await assert_speech_ok("buy porn now", noun="script", workspace_id="w1", where="narration")
    assert "script" in caught.value.detail["message"]
    await assert_speech_ok("A calm script.", noun="script", workspace_id="w1", where="narration")


async def test_narration_is_screened_before_any_voice_provider_is_called():
    from app.pipelines.media.tts_generation import synthesize_speech, synthesize_speech_timed

    for fn in (synthesize_speech, synthesize_speech_timed):
        with pytest.raises(ContentRejected):
            await fn(text="buy porn now", voice_settings=None, workspace_id="w1", user_id="u1")


def test_an_episode_with_unsafe_words_stays_out_of_the_public_feed():
    assert episode_is_safe({"title": "Episode 1", "transcript": [{"word": "hello"}, {"word": "world"}]})
    assert not episode_is_safe({"title": "Episode 2", "transcript": [{"word": "buy"}, {"word": "porn"}]})
    assert not episode_is_safe({"title": "Porn special", "transcript": []})


async def test_an_attached_unsafe_picture_holds_the_post_back_before_sending(make_client):
    client = make_client()
    user = await signup_new_user(client)
    ws = await create_workspace(client, "Attached WS")
    piece = await content_pieces.find_one({"piece_id": await _seed(ws, user["id"])})
    piece["media"] = [{"id": "m1", "kind": "image", "url": "https://example.com/a.png", "mime_type": "image/png"}]
    with patch("app.agents.content_guard.media._fetch", new=AsyncMock(return_value=b"bytes-of-a-picture")), \
         patch("app.shared.llm.call_vision", new=AsyncMock(return_value=UNSAFE_PICTURE)) as vision:
        await check_piece_before_send(piece, ws)
        await check_piece_before_send(piece, ws)
    assert vision.call_count == 1, "the verdict stays with the post"
    block = check_gate({**piece, "approval_status": "approved"}, confirm_anyway=True)
    assert block and block.code == "UNSAFE_CONTENT" and "attached picture" in block.message
    blocker = await schedule_blocker({**piece}, ws)
    assert blocker and blocker[0] == 409


async def test_an_attached_recording_is_judged_by_what_is_said_in_it(make_client):
    client = make_client()
    user = await signup_new_user(client)
    ws = await create_workspace(client, "Recording WS")
    piece = await content_pieces.find_one({"piece_id": await _seed(ws, user["id"])})
    piece["media"] = [{"id": "a1", "kind": "audio", "transcript": [{"word": "buy"}, {"word": "porn"}], "transcript_language": "en"}]
    await check_piece_before_send(piece, ws)
    assert piece["media_safety"]["a1"]["ok"] is False
    assert check_gate({**piece, "approval_status": "approved"}).code == "UNSAFE_CONTENT"


# ── reading speech and frames of video and audio ──────────────────────────────

from types import SimpleNamespace  # noqa: E402

from app.agents.content_guard.media import frame_urls, media_problem, read_upload_speech, transcribe_item  # noqa: E402
from app.db.mongo import media_assets  # noqa: E402


def _words(*tokens):
    return [SimpleNamespace(word=t, start_s=float(i), end_s=float(i) + 0.5) for i, t in enumerate(tokens)]


def test_frames_are_spread_across_a_hosted_video():
    urls = frame_urls("https://res.cloudinary.com/x/video/upload/v1/clip.mp4", 100)
    assert [("so_10.0" in urls[0]), ("so_50.0" in urls[1]), ("so_90.0" in urls[2])] == [True, True, True]
    assert all(u.endswith(".jpg") and "/clip" in u for u in urls)
    assert len(frame_urls("https://res.cloudinary.com/x/video/upload/v1/clip.mp4", None)) == 3
    assert frame_urls("https://example.com/clip.mp4", 10) == []


async def test_a_recording_with_no_transcript_is_transcribed_once_and_saved(make_client):
    client = make_client()
    await signup_new_user(client)
    ws = await create_workspace(client, "Transcribe WS")
    await media_assets.insert_one({"id": "aud1", "workspace_id": ws, "kind": "audio", "url": "https://res.cloudinary.com/x/video/upload/v1/a.mp3"})
    item = {"id": "aud1", "kind": "audio", "url": "https://res.cloudinary.com/x/video/upload/v1/a.mp3"}
    transcriber = AsyncMock(return_value=(_words("buy", "porn", "now"), "en"))
    with patch("app.agents.content_guard.media._fetch", new=AsyncMock(return_value=b"audio-bytes")), \
         patch("app.pipelines.audio.transcriber.transcribe_audio_detailed", new=transcriber):
        record = await media_problem(item, ws)
    assert record["ok"] is False and record["categories"] == ["sexual"]
    assert [w["word"] for w in (await media_assets.find_one({"id": "aud1"}))["transcript"]] == ["buy", "porn", "now"]
    assert transcriber.call_count == 1


async def test_a_clean_video_passes_and_a_silent_one_is_marked_unchecked():
    video = {"id": "v1", "kind": "video", "url": "https://res.cloudinary.com/x/video/upload/v1/v.mp4", "transcript": [{"word": "hello"}, {"word": "there"}]}
    record = await media_problem(dict(video), "w1")
    assert record["ok"] is True and "unchecked" not in record
    silent = {"id": "v2", "kind": "video", "url": "https://res.cloudinary.com/x/video/upload/v1/s.mp4"}
    with patch("app.agents.content_guard.media._fetch", new=AsyncMock(return_value=None)):
        quiet = await media_problem(silent, "w1")
    assert quiet["ok"] is True and quiet["unchecked"]


async def test_sampled_frames_are_read_only_when_ops_turns_them_on():
    video = {"id": "v3", "kind": "video", "url": "https://res.cloudinary.com/x/video/upload/v1/v.mp4", "duration_s": 60,
             "transcript": [{"word": "hello"}], "poster_url": "https://res.cloudinary.com/x/video/upload/so_1/v1/v.jpg"}
    with patch("app.agents.content_guard.media._fetch", new=AsyncMock(side_effect=lambda u: u.encode())), \
         patch("app.shared.llm.call_vision", new=AsyncMock(return_value=SAFE_PICTURE)) as vision:
        await media_problem(dict(video), "w1")
        assert vision.call_count == 1, "only the poster picture while frames are off"
        guard_media._VERDICTS.clear()
        await guard_config.save({"video_frames": True}, expected_version=0, user_id="u")
        vision.reset_mock()
        await media_problem(dict(video), "w1")
        assert vision.call_count == 4, "the poster and three frames"

    guard_media._VERDICTS.clear()

    def one_bad_frame(prompt, image_bytes, mime_type="image/jpeg", model=None):
        return UNSAFE_PICTURE if b"so_30.0" in image_bytes else SAFE_PICTURE

    with patch("app.agents.content_guard.media._fetch", new=AsyncMock(side_effect=lambda u: u.encode())), \
         patch("app.shared.llm.call_vision", new=AsyncMock(side_effect=one_bad_frame)):
        record = await media_problem(dict(video), "w1")
    assert record["ok"] is False and record["categories"] == ["sexual"]


async def test_nothing_is_transcribed_when_live_checks_are_off(monkeypatch):
    monkeypatch.setattr(settings, "CONTENT_GUARD_LIVE_CHECKS", False)
    with patch("app.agents.content_guard.media._fetch", new=AsyncMock()) as fetch:
        assert await transcribe_item({"id": "a", "kind": "audio", "url": "https://x/a.mp3"}, "w1") is None
    assert not fetch.called


async def test_upload_time_reading_refuses_unsafe_speech_and_keeps_safe_words():
    with patch("app.pipelines.audio.transcriber.transcribe_audio_detailed", new=AsyncMock(return_value=(_words("buy", "porn"), "en"))):
        with pytest.raises(ContentRejected) as caught:
            await read_upload_speech(b"bytes", "talk.mp4", noun="video", workspace_id="w1")
    assert "video" in caught.value.detail["message"]
    with patch("app.pipelines.audio.transcriber.transcribe_audio_detailed", new=AsyncMock(return_value=(_words("hello", "world"), "en"))):
        saved = await read_upload_speech(b"bytes", "talk.mp4", noun="video", workspace_id="w1")
    assert [w["word"] for w in saved] == ["hello", "world"]
    with patch("app.pipelines.audio.transcriber.transcribe_audio_detailed", new=AsyncMock(side_effect=RuntimeError("down"))):
        assert await read_upload_speech(b"bytes", "talk.mp4", noun="video", workspace_id="w1") is None


async def test_uploading_a_video_whose_words_fail_returns_the_friendly_error(make_client):
    client = make_client()
    await signup_new_user(client)
    ws = await create_workspace(client, "Video Upload WS")
    with patch("app.pipelines.audio.transcriber.transcribe_audio_detailed", new=AsyncMock(return_value=(_words("buy", "porn"), "en"))):
        res = await client.post("/api/v1/media", headers=H(ws), files={"file": ("v.mp4", b"fake-video-bytes", "video/mp4")})
    assert res.status_code == 422 and res.json()["detail"]["code"] == "CONTENT_REJECTED"
    assert "video" in res.json()["detail"]["message"]


async def test_a_clean_video_upload_keeps_its_transcript_for_later(make_client):
    client = make_client()
    await signup_new_user(client)
    ws = await create_workspace(client, "Video Keep WS")
    uploaded = {"url": "https://res.cloudinary.com/x/video/upload/v1/v.mp4", "width": 10, "height": 10, "duration_s": 2.0}
    with patch("app.pipelines.audio.transcriber.transcribe_audio_detailed", new=AsyncMock(return_value=(_words("hello", "world"), "en"))), \
         patch("app.api.v1.media.upload_file_detailed", new=AsyncMock(return_value=uploaded)):
        res = await client.post("/api/v1/media", headers=H(ws), files={"file": ("v.mp4", b"fake-video-bytes", "video/mp4")})
    assert res.status_code == 201
    stored = await media_assets.find_one({"id": res.json()["id"]})
    assert [w["word"] for w in stored["transcript"]] == ["hello", "world"]
