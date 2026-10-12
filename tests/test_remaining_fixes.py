"""The remaining edge-case fixes: pure rules first, then the routes that need the test database."""
import io
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import numpy as np
import pytest
import soundfile as sf
from pydantic import ValidationError

from tests.conftest import create_workspace, invite_and_accept, signup_new_user, unique_email


# ── Pure rules ────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_log_lines_hide_keys_and_tokens():
    from app.core.logger import _mask_secrets

    line = _mask_secrets("GET https://x.test/a?key=SECRET123&b=1 failed; Authorization: Bearer abcdefghijklmnop12345")
    assert "SECRET123" not in line and "abcdefghijklmnop12345" not in line
    assert "b=1" in line


def test_a_website_must_be_a_web_address_and_a_timezone_must_be_real():
    from app.models.user import UserUpdateBody

    assert UserUpdateBody(website="https://example.com").website == "https://example.com"
    with pytest.raises(ValidationError):
        UserUpdateBody(website="javascript:alert(1)")
    with pytest.raises(ValidationError):
        UserUpdateBody(timezone="Not/AZone")
    assert UserUpdateBody(timezone="Asia/Kolkata").timezone == "Asia/Kolkata"


def test_a_workspace_name_of_spaces_is_refused():
    from app.models.workspace import CreateWorkspaceBody

    with pytest.raises(ValidationError):
        CreateWorkspaceBody(name="   ", tier="duo")


def test_a_video_clip_must_end_after_it_starts():
    from app.models.audio_asset import MakeVideoRequest

    assert MakeVideoRequest(start_s=1, end_s=5).end_s == 5
    with pytest.raises(ValidationError):
        MakeVideoRequest(start_s=10, end_s=5)
    with pytest.raises(ValidationError):
        MakeVideoRequest(start_s=-1)


def test_a_dialogue_voice_id_must_be_a_plain_id_and_the_script_is_bounded():
    from app.api.v1.audio_assets import DialogueTurn, GenerateDialogueRequest

    assert DialogueTurn(speaker="A", text="hi", voice_id="abc_123-X").voice_id == "abc_123-X"
    with pytest.raises(ValidationError):
        DialogueTurn(speaker="A", text="hi", voice_id="../v1/user")
    with pytest.raises(ValidationError):
        GenerateDialogueRequest(title="t", brand_id="b", turns=[DialogueTurn(speaker="A", text="x" * 20000)])


def test_the_feed_drops_characters_xml_cannot_hold():
    from app.api.v1.audio_assets import _escape_xml

    assert _escape_xml("Hello" + chr(0) + chr(8) + " & <you>") == "Hello &amp; &lt;you&gt;"


def test_a_silent_recording_is_not_turned_into_loud_noise():
    from app.pipelines.media.audio_enhance import enhance_audio

    out = io.BytesIO()
    sf.write(out, np.zeros(16000 * 2, dtype="float32"), 16000, format="WAV", subtype="PCM_16")
    wav, settings = enhance_audio(out.getvalue(), "audio/wav")
    assert settings["loudness_normalize"] is False and settings["measured_loudness_before_lufs"] is None
    data, _ = sf.read(io.BytesIO(wav))
    assert float(np.abs(data).max()) < 0.01


def test_a_long_attachment_name_keeps_its_extension():
    from app.shared.support_files import _clean_name

    name = "a" * 300 + ".pdf"
    cleaned = _clean_name(name)
    assert len(cleaned) <= 120 and cleaned.endswith(".pdf")


def test_csv_cells_that_start_like_a_formula_are_made_plain():
    from app.shared.support_privacy import _plain

    assert _plain("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert _plain(None) == "" and _plain("fine") == "fine"


def test_leaving_waiting_clears_the_reminders_and_entering_it_starts_the_clock():
    from app.shared.support import transition_fields

    now = datetime.now(timezone.utc)
    into = transition_fields("investigating", "waiting_on_member", now)
    assert into["waiting_since"] == now
    out = transition_fields("waiting_on_member", "investigating", now)
    assert out["reminder_sent_at"] is None and out["close_warning_sent_at"] is None and out["waiting_since"] is None


# ── Routes ────────────────────────────────────────────────────────────────────────────────────────────────────────────
async def test_giving_someone_the_owner_role_is_a_hand_over(make_client, api_client):
    from app.db.mongo import workspaces

    me = await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Hand over", tier="duo")
    other_client, other = await invite_and_accept(api_client, make_client, ws, "admin")
    res = await api_client.put(f"/api/v1/workspaces/{ws}/members/{other['id']}/role", json={"role": "owner"})
    assert res.status_code == 200, res.text
    assert (await workspaces.find_one({"id": ws}))["owner_id"] == other["id"]
    # the new owner can hand it back; the old owner is now an admin and cannot take it by themselves
    again = await api_client.put(f"/api/v1/workspaces/{ws}/members/{me['id']}/role", json={"role": "owner"})
    assert again.status_code in (403, 400)


async def test_the_only_voice_cannot_be_deleted(api_client):
    await signup_new_user(api_client)
    created = await api_client.post("/api/v1/brand/", json={"brand_type": "Person"})
    brand_id = created.json()["brand_profile_id"]
    res = await api_client.delete(f"/api/v1/brand/{brand_id}")
    assert res.status_code == 400 and "only voice" in res.json()["detail"]


async def test_repurposes_only_note_results_that_exist(api_client):
    await signup_new_user(api_client)
    res = await api_client.post(
        "/api/v1/repurposes",
        json={"source_kind": "text", "source_id": "src", "outputs": [{"kind": "image", "id": "does-not-exist", "label": "x"}]},
    )
    assert res.status_code in (200, 201)
    assert res.json()["recorded"] == 0


async def test_a_ticket_reply_is_refused_when_the_ticket_changed_meanwhile(api_client):
    from app.db.mongo import support_tickets

    await signup_new_user(api_client)
    created = await api_client.post(
        "/api/v1/support/tickets",
        json={"subject": "Something is wrong", "description": "Details here", "category": "Other", "severity": "P2"},
    )
    assert created.status_code in (200, 201), created.text
    ticket_id = created.json()["id"]
    first = await api_client.post(f"/api/v1/support/tickets/{ticket_id}/messages", json={"text": "More detail"})
    assert first.status_code == 200, first.text
    await support_tickets.update_one({"id": ticket_id}, {"$set": {"status": "closed"}})
    closed = await api_client.post(f"/api/v1/support/tickets/{ticket_id}/messages", json={"text": "Too late"})
    assert closed.status_code == 409


async def test_a_second_open_filing_of_the_same_kind_is_refused(api_client):
    await signup_new_user(api_client)
    body = {"subject": "Same thing", "description": "Details here", "category": "Billing", "severity": "P2"}
    first = await api_client.post("/api/v1/support/tickets", json=body)
    assert first.status_code in (200, 201), first.text
    second = await api_client.post("/api/v1/support/tickets", json=body)
    assert second.status_code == 409


def test_a_timeout_message_is_never_read_as_retryable():
    from app.pipelines.publish.supervisor.classifier import ErrorType, TIMEOUT_NOTE, classify_error, is_timeout

    assert is_timeout(408, "") and is_timeout(None, "Request timed out") and not is_timeout(500, "boom")
    assert classify_error(408, TIMEOUT_NOTE) == ErrorType.FATAL


def test_the_picture_choice_is_on_by_default_and_can_be_turned_off():
    from app.models.campaign import CampaignTextOptions
    from app.models.text import ExtrasConfig

    assert ExtrasConfig().default_picture is True and CampaignTextOptions().default_picture is True
    assert ExtrasConfig(default_picture=False).default_picture is False


def test_a_dialogue_turn_voiced_before_is_reused():
    from app.api.v1.audio_assets import _TURN_CACHE, _remember_turn, _turn_cache_key

    class Speech:
        audio = b"x" * 10

    key = _turn_cache_key("w", "u", "Hello there", "member")
    _remember_turn(key, Speech())
    assert _TURN_CACHE.get(key) is not None
    assert _turn_cache_key("w", "u", "Hello there", "member") == key
    assert _turn_cache_key("w", "u", "Hello there", "voice-2") != key


async def test_a_new_invite_for_staff_keeps_the_old_code_until_the_new_one_is_sent():
    from app.db.mongo import signup_invites
    from app.shared import invites

    email = unique_email()
    lead = {"id": str(uuid4()), "email": email}
    old, _ = await invites.create_invite(lead, None, "test")
    new, _ = await invites.create_invite(lead, None, "test", supersede=False)
    assert (await signup_invites.find_one({"id": old["id"]}))["revoked"] is False
    await invites.supersede_earlier(email, new["id"])
    assert (await signup_invites.find_one({"id": old["id"]}))["revoked"] is True
    assert (await signup_invites.find_one({"id": new["id"]}))["revoked"] is False
    await signup_invites.delete_many({"email": email})
