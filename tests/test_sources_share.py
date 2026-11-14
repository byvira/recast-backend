"""Something shared from the phone's share sheet is saved as a plain, free draft. No model is used; the link is kept as text and never opened."""
from app.sources import flow, runner, share
from tests.test_sources_service import world  # noqa: F401  (world is a fixture)


def test_the_shared_link_is_found_in_the_link_field_or_in_the_text_and_trailing_marks_are_dropped():
    assert share.first_link("https://example.com/a", "other https://x.com") == "https://example.com/a"
    assert share.first_link(None, "Read this (https://example.com/story?id=3), it is good.") == "https://example.com/story?id=3"
    assert share.first_link(None, "no link here", None) == ""


def test_the_saved_words_are_the_title_the_text_without_the_link_and_the_link_with_no_repeats():
    assert share.draft_text("A good read", "Worth your time", "https://e.com/a") == "A good read\n\nWorth your time\n\nhttps://e.com/a"
    assert share.draft_text("https://e.com/a", "https://e.com/a", "https://e.com/a") == "https://e.com/a"                 # an app that sends the link three times gives it once
    assert share.draft_text("Same words", "Same words", "") == "Same words"
    assert len(share.draft_text("t", "x" * 9000, "")) == share.MAX_TEXT


async def _share(client, h, brand, **over):
    return await client.post("/api/v1/sources/share", headers=h, json={"brand_id": brand, "platform": "LinkedIn", **over})


async def test_a_shared_link_becomes_a_free_draft_through_the_normal_create_tool(world, monkeypatch):
    client, user, ws, brand, h = world
    calls = []

    def fake_runner(workspace_id, user_id):
        async def run_tool(name, arguments):
            calls.append((workspace_id, user_id, name, arguments))
            return {"data": {"piece_id": "piece-1"}, "run": None}

        return run_tool

    monkeypatch.setattr(runner, "tool_runner", fake_runner)
    made = await _share(client, h, brand, title="Coffee trends", text="Have a look at https://example.com/coffee today")
    assert made.status_code == 200, made.text
    assert made.json() == {"saved": True, "piece_id": "piece-1", "link": "https://example.com/coffee"}
    (workspace_id, user_id, name, arguments), = calls
    assert (workspace_id, user_id, name) == (ws, user, "library.create_draft")                    # only the plain draft tool, as the member
    assert arguments["platform"] == "LinkedIn" and arguments["brand_id"] == brand
    content = arguments["content"]
    assert content.startswith("Coffee trends\n\nHave a look at") and content.endswith("https://example.com/coffee") and content.count("https://example.com/coffee") == 1


async def test_nothing_shared_a_wrong_brand_a_missing_platform_or_a_refused_draft_each_say_so_plainly(world, monkeypatch):
    client, user, ws, brand, h = world
    assert (await _share(client, h, brand)).status_code == 422
    assert "Nothing was shared" in (await _share(client, h, brand, title=" ", text="")).text
    assert (await _share(client, h, "no-such-brand", text="hello")).status_code == 422
    assert (await client.post("/api/v1/sources/share", headers=h, json={"brand_id": brand, "text": "hello", "platform": ""})).status_code == 422

    def refusing(workspace_id, user_id):
        async def run_tool(name, arguments):
            raise flow.SourceToolError("tool_off", "Drafts are switched off for now.", stop=True)

        return run_tool

    monkeypatch.setattr(runner, "tool_runner", refusing)
    refused = await _share(client, h, brand, text="hello")
    assert refused.status_code == 403 and "switched off" in refused.text


async def test_a_person_outside_the_workspace_cannot_save_a_shared_link(world, signup_user):
    client, user, ws, brand, h = world
    other, _ = await signup_user()
    assert (await other.post("/api/v1/sources/share", headers=h, json={"brand_id": brand, "platform": "LinkedIn", "text": "hello"})).status_code in (401, 403, 404)
