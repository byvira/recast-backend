"""The History lists of the four pipelines share one search, status filter and sort, and each applies it on the server so paging
stays right: a search never shows only what happens to be on the loaded page."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db.mongo import audio_assets, content_sessions, image_assets
from app.shared import history_query as H
from tests.conftest import create_workspace

HEAD = lambda ws: {"X-Workspace-Id": ws}  # noqa: E731


def test_the_helpers_build_the_same_rules_for_every_list():
    assert H.search_clause("  ", ("title",)) is None
    clause = H.search_clause("Spring (sale)", ("title", "input_text"))
    assert [list(c)[0] for c in clause["$or"]] == ["title", "input_text"], "each field is searched"
    assert "\\(sale\\)" in clause["$or"][0]["title"]["$regex"], "typed characters are never read as a pattern"
    assert H.status_clause("approved") == {"approval_status": "approved"}
    assert H.status_clause("pending") == {"approval_status": {"$nin": ["approved", "rejected"]}}
    assert H.status_clause(None) is None
    assert H.sort_spec("oldest")[0] == ("created_at", 1) and H.sort_spec("title")[0] == ("title", 1) and H.sort_spec("x")[0] == ("created_at", -1)
    assert H.add_clauses({"a": 1}) == {"a": 1} and H.add_clauses({"a": 1}, None, {"b": 2}) == {"$and": [{"a": 1}, {"b": 2}]}
    rows = [{"id": "1", "title": "b", "created_at": "2"}, {"id": "2", "title": "A", "created_at": "3"}, {"id": "3", "title": "c", "created_at": "1"}]
    assert [r["id"] for r in H.in_memory_sort(rows, "newest")] == ["2", "1", "3"]
    assert [r["id"] for r in H.in_memory_sort(rows, "oldest")] == ["3", "1", "2"]
    assert [r["id"] for r in H.in_memory_sort(rows, "title")] == ["2", "1", "3"]


async def _seed_images(ws: str) -> None:
    now = datetime.now(timezone.utc)
    for i, (title, status) in enumerate([("Spring sale card", "approved"), ("Autumn quote", "pending"), ("Spring teaser", "rejected"), ("Winter note", None)]):
        await image_assets.insert_one({
            "id": uuid4().hex, "workspace_id": ws, "brand_id": "b", "title": title, "version_count": 1, "slides": [],
            **({"approval_status": status} if status else {}), "created_at": now - timedelta(days=10 - i),
        })


async def test_the_picture_history_searches_filters_and_sorts_on_the_server(signup_user):
    client, _ = await signup_user("Maker")
    ws = await create_workspace(client, f"History {uuid4().hex[:6]}")
    await _seed_images(ws)

    def titles(res):
        assert res.status_code == 200, res.text
        return [i["title"] for i in res.json()["items"]], res.json()["total"]

    base = "/api/v1/image-assets/"
    assert titles(await client.get(base, headers=HEAD(ws)))[0][0] == "Winter note", "newest first by default"
    assert titles(await client.get(base, params={"sort": "oldest"}, headers=HEAD(ws)))[0][0] == "Spring sale card"
    assert titles(await client.get(base, params={"sort": "title"}, headers=HEAD(ws)))[0][0] == "Autumn quote"
    found, total = titles(await client.get(base, params={"q": "spring"}, headers=HEAD(ws)))
    assert total == 2 and set(found) == {"Spring sale card", "Spring teaser"}
    assert titles(await client.get(base, params={"status": "approved"}, headers=HEAD(ws))) == (["Spring sale card"], 1)
    pending, n = titles(await client.get(base, params={"status": "pending"}, headers=HEAD(ws)))
    assert n == 2 and set(pending) == {"Autumn quote", "Winter note"}, "work that never had a status is waiting too"
    both, n = titles(await client.get(base, params={"q": "spring", "status": "rejected"}, headers=HEAD(ws)))
    assert (both, n) == (["Spring teaser"], 1)
    # paging is counted after the search, so page two of a search is right
    page2, n = titles(await client.get(base, params={"q": "spring", "limit": 1, "skip": 1, "sort": "oldest"}, headers=HEAD(ws)))
    assert (page2, n) == (["Spring teaser"], 2)
    assert (await client.get(base, params={"sort": "sideways"}, headers=HEAD(ws))).status_code == 422
    assert (await client.get(base, params={"q": "x" * 500}, headers=HEAD(ws))).status_code == 422


async def test_the_audio_and_video_histories_take_the_same_options(signup_user):
    client, _ = await signup_user("Maker")
    ws = await create_workspace(client, f"History {uuid4().hex[:6]}")
    now = datetime.now(timezone.utc)
    for i, (title, status) in enumerate([("Launch episode", "approved"), ("Pricing chat", "pending")]):
        await audio_assets.insert_one({
            "id": uuid4().hex, "workspace_id": ws, "brand_id": "b", "title": title, "version_count": 1, "approval_status": status,
            "created_at": now - timedelta(days=5 - i),
            "video_clips": [{"id": uuid4().hex, "media_id": "m", "style": "cover", "size": "square", "title": f"{title} short", "created_at": now - timedelta(days=5 - i)}],
        })
    audio = (await client.get("/api/v1/audio-assets/", params={"q": "pricing"}, headers=HEAD(ws))).json()
    assert [a["title"] for a in audio["items"]] == ["Pricing chat"] and audio["total"] == 1
    assert (await client.get("/api/v1/audio-assets/", params={"status": "approved"}, headers=HEAD(ws))).json()["total"] == 1
    clips = await client.get("/api/v1/audio-assets/video-clips", params={"q": "launch"}, headers=HEAD(ws))
    assert clips.status_code == 200 and [c["title"] for c in clips.json()["items"]] == ["Launch episode short"]
    by_recording = (await client.get("/api/v1/audio-assets/video-clips", params={"q": "pricing chat", "status": "pending"}, headers=HEAD(ws))).json()
    assert by_recording["total"] == 1, "a video is found by its recording's name too"
    oldest = (await client.get("/api/v1/audio-assets/video-clips", params={"sort": "oldest"}, headers=HEAD(ws))).json()["items"]
    assert oldest[0]["title"] == "Launch episode short"


async def test_the_text_run_history_searches_the_name_and_the_start_of_the_input(signup_user):
    client, profile = await signup_user("Maker")
    ws = await create_workspace(client, f"History {uuid4().hex[:6]}")
    now = datetime.now(timezone.utc)
    for i, (title, text) in enumerate([("Launch thread", "Announcing our new plan"), ("", "Tips for founders"), ("Recap", "Weekly numbers")]):
        await content_sessions.insert_one({
            "session_id": uuid4().hex, "workspace_id": ws, "user_id": profile["id"], "title": title, "input_text": text,
            "created_at": now - timedelta(days=3 - i),
        })
    res = await client.get("/api/v1/content/sessions", params={"q": "founders"}, headers=HEAD(ws))
    assert res.status_code == 200 and res.json()["total"] == 1, "found by the start of its input when it has no name"
    assert [s["title"] for s in (await client.get("/api/v1/content/sessions", params={"q": "launch"}, headers=HEAD(ws))).json()["items"]] == ["Launch thread"]
    newest = (await client.get("/api/v1/content/sessions", headers=HEAD(ws))).json()["items"]
    assert newest[0]["title"] == "Recap"
    oldest = (await client.get("/api/v1/content/sessions", params={"sort": "oldest"}, headers=HEAD(ws))).json()["items"]
    assert oldest[0]["title"] == "Launch thread"


def test_the_remy_word_lists_are_tidied_limited_and_listed_once():
    import pytest
    from pydantic import ValidationError

    from app.models.lexicon import MAX_WORD_CHARS, MAX_WORDS, MemberLexiconWrite

    tidy = MemberLexiconWrite(whitelist=["  synergy ", "Synergy", "", "  growth   hacking "], blacklist=["leverage", "LEVERAGE"])
    assert tidy.whitelist == ["synergy", "growth hacking"], "trimmed, spaces folded, listed once whatever the case, blanks dropped"
    assert tidy.blacklist == ["leverage"]
    with pytest.raises(ValidationError):
        MemberLexiconWrite(whitelist=["x" * (MAX_WORD_CHARS + 1)])
    with pytest.raises(ValidationError):
        MemberLexiconWrite(blacklist=[f"word{i}" for i in range(MAX_WORDS + 1)])
