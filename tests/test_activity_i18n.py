"""Activity, Inbox, Remy and Odette rows read in their reader's language: sentences translated with names kept as they are, free
text such as errors translated too, Remy's and Odette's own words left as written, English never sent to the translator, and rows
written again after a language change. The translator is a stand-in here: no real model call is made."""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.db.mongo import activity_entries, workspaces
from app.shared import localized_strings
from app.shared.activity import i18n
from app.shared.activity.store import get_entry, patch_entry, upsert_entry
from tests.conftest import create_workspace, signup_new_user


@pytest.fixture
def translator(monkeypatch):
    """Marks what it translates with the language, keeping every {slot}, and counts its calls."""
    calls = []

    async def fake(key, language, english_template):
        calls.append(english_template)
        return f"[{language}] {english_template}", True

    monkeypatch.setattr(localized_strings, "_translate", fake)
    return calls


async def _workspace(api_client, language):
    await signup_new_user(api_client)
    ws = await create_workspace(api_client, f"Lang {uuid4().hex[:5]}", tier="large")
    if language:
        await workspaces.update_one({"id": ws}, {"$set": {"language": language}})
    return ws


def _row(ws, **fields):
    from datetime import datetime, timezone

    return {"_id": f"test:{uuid4().hex}", "workspace_id": ws, "lane": "passive", "visibility": "workspace", "status": "success",
            "occurred_at": datetime.now(timezone.utc), "category": "post_published", **fields}


async def test_a_row_is_written_in_the_workspace_language_with_names_kept(api_client, translator):
    ws = await _workspace(api_client, "Malayalam")
    row = _row(ws, title=i18n.phrase("Scheduled post to {platform} failed", platform="LinkedIn"),
               description=i18n.phrase("{reason} Trying again in {minutes} min.", reason=i18n.words("The token expired."), minutes=5),
               metadata={"severity": "Warning"})
    await upsert_entry(row)
    saved = await get_entry(row["_id"])
    assert saved["title"] == "[Malayalam] Scheduled post to LinkedIn failed"
    assert saved["description"] == "[Malayalam] [Malayalam] The token expired. Trying again in 5 min."
    assert saved["metadata"]["severity"] == "[Malayalam] Warning"
    assert "scheduled post to linkedin failed" in saved["search_text"], "an English search still finds it"
    assert "{platform}" in translator[0], "the name goes in a slot, never into the words sent to the translator"


async def test_words_already_in_the_readers_language_are_left_alone_and_english_is_not_translated(api_client, translator):
    ws = await _workspace(api_client, "Hindi")
    row = _row(ws, title=i18n.as_written("पहले से हिंदी में"), description=i18n.as_written("Remy का संदेश"))
    await upsert_entry(row)
    saved = await get_entry(row["_id"])
    assert saved["title"] == "पहले से हिंदी में" and saved["description"] == "Remy का संदेश"
    english_ws = await _workspace(api_client, None)
    await upsert_entry(_row(english_ws, title=i18n.phrase("Published to {platform}", platform="Threads"), description="Post went live."))
    assert translator == [], "nothing goes to the translator for an English reader or for words already written"


async def test_a_language_change_writes_recent_rows_again(api_client, translator):
    ws = await _workspace(api_client, None)
    row = _row(ws, title=i18n.phrase("Member joined the workspace"), description=i18n.phrase("{name} joined as {role}.", name="Asha", role="editor"))
    await upsert_entry(row)
    assert (await get_entry(row["_id"]))["title"] == "Member joined the workspace"
    await workspaces.update_one({"id": ws}, {"$set": {"language": "Tamil"}})
    assert await i18n.relocalize(ws) == 1
    saved = await get_entry(row["_id"])
    assert saved["title"] == "[Tamil] Member joined the workspace" and saved["description"] == "[Tamil] Asha joined as editor."
    assert await i18n.relocalize(ws) == 0, "a row already in the reader's language is not written again"


async def test_a_decision_label_follows_the_rows_language(api_client, translator):
    ws = await _workspace(api_client, "Kannada")
    row = _row(ws, title=i18n.phrase("Brand voice updated"), metadata={"decision": "Pending"})
    await upsert_entry(row)
    await patch_entry(row["_id"], {"metadata": {"decision": "Accepted"}})
    saved = await activity_entries.find_one({"_id": row["_id"]})
    assert saved["metadata"]["decision"] == "[Kannada] Accepted"
    assert saved["i18n"]["sources"]["title"]["$t"] == "Brand voice updated", "a saved row keeps its English source"


async def test_a_failed_translation_keeps_the_english(api_client, monkeypatch):
    async def broken(key, language, english_template):
        raise RuntimeError("the translator is down")

    monkeypatch.setattr(localized_strings, "_translate", broken)
    ws = await _workspace(api_client, "Bengali")
    row = _row(ws, title=i18n.phrase("Published to {platform}", platform="Bluesky"))
    await upsert_entry(row)
    assert (await get_entry(row["_id"]))["title"] == "Published to Bluesky"
