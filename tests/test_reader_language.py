"""The one place that decides which language a person is written to: the order for each audience, saying a sentence in that language,
plain error messages, the email template that fits the reader and a lock-screen notice per person. No real model is called."""
from unittest.mock import AsyncMock, patch

import pytest

from app.db.mongo import users, workspaces
from app.shared import reader_language as RL
from tests.conftest import create_workspace


@pytest.fixture(autouse=True)
def _fresh():
    RL.forget()
    RL._template_names = (0.0, frozenset())
    yield
    RL.forget()


# ---- which language -----------------------------------------------------------------------------------------------------------
async def _setup(signup_user, member_language, workspace_language, owner_language):
    owner, owner_profile = await signup_user("Owner")
    ws = await create_workspace(owner, "Language test workspace")
    await users.update_one({"id": owner_profile["id"]}, {"$set": {"language": owner_language}})
    await workspaces.update_one({"id": ws}, {"$set": {"language": workspace_language, "owner_id": owner_profile["id"]}})
    member, member_profile = await signup_user("Member")
    await users.update_one({"id": member_profile["id"]}, {"$set": {"language": member_language}})
    return ws, owner_profile["id"], member_profile["id"]


async def test_each_audience_is_read_in_its_own_order(signup_user):
    ws, owner_id, member_id = await _setup(signup_user, "ta", "hi", "te")
    assert await RL.reader_language("member", user_id=member_id, workspace_id=ws) == "ta", "a person's own language first"
    assert await RL.reader_language("workspace", workspace_id=ws) == "hi", "a workspace's language first"
    assert await RL.reader_language("owner", workspace_id=ws) == "te", "the owner's own language first"
    assert await RL.reader_language("member", user_id=member_id, workspace_id=ws, explicit="ml") == "ml"


async def test_nothing_known_means_english():
    assert await RL.reader_language("member", user_id="nobody", workspace_id="nowhere") == "en"
    assert await RL.reader_language("workspace", workspace_id=None) == "en"


def test_english_and_blends_are_read_in_english():
    for code in (None, "", "en", "en-IN", "ta+en", "hi+en"):
        assert RL.wants_english(code)
    for code in ("hi", "ta", "te", "xx"):
        assert not RL.wants_english(code)


# ---- saying it ------------------------------------------------------------------------------------------------------------------
async def test_english_costs_no_translation():
    with patch("app.shared.localized_strings.get_localized_string", new=AsyncMock()) as translate:
        assert await RL.say("k", "Hello {name}", "en", {"name": "Asha"}) == "Hello Asha"
        assert await RL.say("k", "Hello {name}", "ta+en", {"name": "Asha"}) == "Hello Asha"
    assert not translate.called


async def test_another_language_is_translated_and_a_failure_keeps_the_english():
    async def fake(key, language, template, ctx=None):
        return f"[{language}] " + template.format(**(ctx or {}))

    with patch("app.shared.localized_strings.get_localized_string", new=fake):
        assert await RL.say("k", "Hello {name}", "hi-IN", {"name": "Asha"}) == "[hi] Hello Asha"

    async def broken(*a, **k):
        raise RuntimeError("no model")

    with patch("app.shared.localized_strings.get_localized_string", new=broken):
        assert await RL.say("k", "Hello {name}", "hi", {"name": "Asha"}) == "Hello Asha"


async def test_a_slow_translation_does_not_hold_the_reader_up():
    import asyncio

    async def slow(key, language, template, ctx=None):
        await asyncio.sleep(0.3)
        return "late"

    with patch("app.shared.localized_strings.get_localized_string", new=slow):
        assert await RL.say("k", "Hello", "hi", wait=0.05) == "Hello"
        await asyncio.sleep(0.4)  # it finishes in the background, so it is remembered for the next time


# ---- plain error messages --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, static", [
    ("You don't have access to this workspace.", False),  # an apostrophe can mean a name or a quote, so it is left alone
    ("You do not have access to this workspace.", True),
    ("The post was not found.", True),
    ("Post 42 was not found.", False),
    ("Link https://x.com is not allowed", False),
    ("ab", False),
    ("x" * 300, False),
])
def test_only_plain_sentences_are_translated(text, static):
    assert RL.is_static_message(text) is static


async def test_an_error_keeps_its_exact_words_unless_it_is_plain_and_the_reader_is_not_english():
    async def fake(key, language, template, ctx=None):
        return "[hi] " + template

    with patch("app.shared.localized_strings.get_localized_string", new=fake):
        assert await RL.say_error("The post was not found.", "hi") == "[hi] The post was not found."
        assert await RL.say_error("The post was not found.", "en") == "The post was not found."
        assert await RL.say_error("Post 42 was not found.", "hi") == "Post 42 was not found."
        assert await RL.say_error({"code": "x"}, "hi") == {"code": "x"}


async def test_the_api_answers_in_the_readers_language_but_not_for_staff_routes(signup_user):
    ws, owner_id, member_id = await _setup(signup_user, "hi", "hi", "hi")
    client, profile = await signup_user("Reader")
    await users.update_one({"id": profile["id"]}, {"$set": {"language": "hi"}})
    ws = await create_workspace(client, "Reader workspace")

    async def fake(key, language, template, ctx=None):
        return "[hi] " + template

    with patch("app.shared.localized_strings.get_localized_string", new=fake):
        missing = await client.post("/api/v1/content-guard/review-requests", json={"piece_id": "nope"}, headers={"X-Workspace-Id": ws})
        assert missing.status_code in (403, 404), missing.text
        detail = missing.json()["detail"]
        assert isinstance(detail, str)
        if missing.status_code == 404:
            assert detail == "[hi] That post was not found."
        staff = await client.get("/api/v1/ops/quality")
        assert staff.status_code == 403 and not str(staff.json()["detail"]).startswith("[hi]"), "Ops stays in English"


async def test_an_english_reader_gets_the_message_as_before(signup_user):
    client, profile = await signup_user("English reader")
    res = await client.get("/api/v1/ops/quality")
    assert res.status_code == 403 and isinstance(res.json()["detail"], str) and not res.json()["detail"].startswith("[")


# ---- email templates ---------------------------------------------------------------------------------------------------------------
async def test_the_reader_gets_their_own_template_when_one_is_published_otherwise_english():
    published = frozenset({"billing-receipt", "billing-receipt-hi"})
    assert await RL.template_for("billing-receipt", "hi", published) == "billing-receipt-hi"
    assert await RL.template_for("billing-receipt", "hi-IN", published) == "billing-receipt-hi"
    assert await RL.template_for("billing-receipt", "ta", published) == "billing-receipt", "no Tamil template yet"
    assert await RL.template_for("billing-receipt", "en", published) == "billing-receipt"
    assert await RL.template_for("billing-receipt", None, published) == "billing-receipt"
    assert await RL.template_for("billing-receipt", "ta+en", published) == "billing-receipt"


async def test_an_email_is_sent_with_the_language_template_chosen(monkeypatch):
    from app.core import notifications
    from app.core.config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(RL, "_template_names", (10**12, frozenset({"billing-receipt", "billing-receipt-hi"})))
    sent = []

    class FakeEmails:
        @staticmethod
        def send(payload):
            sent.append(payload)

    import resend

    monkeypatch.setattr(resend, "Emails", FakeEmails)
    assert await notifications.send_templated_email("billing-receipt", "a@b.com", {"n": 1}, language="hi")
    assert await notifications.send_templated_email("billing-receipt", "a@b.com", {"n": 1}, language="ta")
    assert await notifications.send_templated_email("billing-receipt", "a@b.com", {"n": 1})
    assert [p["template"]["id"] for p in sent] == ["billing-receipt-hi", "billing-receipt", "billing-receipt"]


# ---- lock-screen notices ------------------------------------------------------------------------------------------------------------
async def test_each_person_gets_the_notice_in_their_own_language():
    from app.shared import push
    from app.shared.activity.i18n import phrase

    sources = {"title": phrase("Scheduled post to {platform} failed", platform="LinkedIn"), "description": phrase("Try again from Up next.")}

    async def fake(key, language, template, ctx=None):
        return f"[{language}] " + template.format(**(ctx or {}))

    with patch("app.shared.localized_strings.get_localized_string", new=fake):
        title, body = await push._in_language(sources, "hi", "Scheduled post to LinkedIn failed", "Try again from Up next.")
        assert title.startswith("[hi]") and "LinkedIn" in title and body.startswith("[hi]")
        assert await push._in_language(sources, "en", "T", "B") == ("T", "B")
        assert await push._in_language({}, "hi", "T", "B") == ("T", "B"), "a row with no English source keeps its words"
