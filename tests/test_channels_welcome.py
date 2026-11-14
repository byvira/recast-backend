"""The welcome sent when a chat is linked: it names the workspace, says what can be sent and the handy words, and is written in the member's language."""
from app.channels import engine, phrases
from app.shared import reader_language


async def test_the_english_welcome_names_the_workspace_and_lists_what_to_send_and_the_handy_words():
    text = await phrases.linked_text("en", "Spring Studio")
    assert "Spring Studio" in text and "{workspace}" not in text
    assert "a voice note" in text and "a link" in text and "a line of text" in text
    assert all(word in text for word in ("NEW", "REMIND ON", "STOP"))
    assert "Nothing is posted without your yes" in text


async def test_hindi_and_tamil_are_written_out_and_name_the_workspace():
    for code in ("hi", "ta"):
        text = await phrases.linked_text(code, "Spring Studio")
        assert "Spring Studio" in text and "{workspace}" not in text
        assert all(word in text for word in ("NEW", "REMIND ON", "STOP"))
        assert text != await phrases.linked_text("en", "Spring Studio")


async def test_a_missing_workspace_name_becomes_your_workspace_in_the_same_language():
    assert "your workspace" in await phrases.linked_text("en", None)
    assert "आपका वर्कस्पेस" in await phrases.linked_text("hi", "  ")


async def test_another_language_is_translated_with_the_name_filled_in(monkeypatch):
    seen: dict = {}

    async def fake_say(key, english, language, ctx=None, **kw):
        seen.update(key=key, english=english, language=language, ctx=ctx)
        return "TRANSLATED"

    monkeypatch.setattr(reader_language, "say", fake_say)
    assert await phrases.linked_text("fr", "Spring Studio") == "TRANSLATED"
    assert seen["key"] == "channel.linked" and seen["language"] == "fr" and seen["ctx"] == {"name": "Spring Studio"}
    assert "{name}" in seen["english"] and "{workspace}" not in seen["english"]


async def test_the_welcome_uses_the_members_language_then_the_workspaces(monkeypatch):
    asked: dict = {}

    async def fake_language(audience, *, user_id=None, workspace_id=None, explicit=None, text=None):
        asked.update(audience=audience, user_id=user_id, workspace_id=workspace_id)
        return "hi"

    class FakeWorkspaces:
        async def find_one(self, query, projection=None):
            return {"name": "Spring Studio"}

    monkeypatch.setattr(reader_language, "reader_language", fake_language)
    monkeypatch.setattr("app.db.mongo.workspaces", FakeWorkspaces())
    text = await engine._welcome({"user_id": "u1", "workspace_id": "w1"}, "en")
    assert asked == {"audience": "member", "user_id": "u1", "workspace_id": "w1"} and "Spring Studio" in text
    assert text == await phrases.linked_text("hi", "Spring Studio")


async def test_when_the_language_cannot_be_found_the_welcome_still_goes_in_the_language_typed(monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("no database")

    monkeypatch.setattr(reader_language, "reader_language", broken)
    text = await engine._welcome({"user_id": "u1", "workspace_id": "w1"}, "ta")
    assert text == await phrases.linked_text("ta", None)
