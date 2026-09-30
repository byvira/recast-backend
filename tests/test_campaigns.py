"""Tests for /api/v1/campaigns — Phase 1 of the "bulk campaigns" architecture.

A campaign groups multiple generation runs under one tracked entity, with
real aggregate progress computed from the pieces it generated. The
Campaign model previously existed as a dead scaffold with no API route
anywhere. Phase 1 is text-only, single-platform-set per campaign.

Runs with the LLM mocked — never a real Groq call.
"""

from tests.conftest import create_workspace, invite_and_accept, signup_new_user


async def _create_brand(client, ws_id: str | None = None) -> str:
    headers = {"X-Workspace-Id": ws_id} if ws_id else {}
    res = await client.post("/api/v1/brand/", json={"brand_type": "Person"}, headers=headers)
    assert res.status_code in (200, 201), res.text
    brand_id = res.json()["brand_profile_id"]
    from app.db.mongo import brand_profiles
    await brand_profiles.update_one({"id": brand_id}, {"$set": {"is_complete": True}})
    return brand_id


def _valid_body(brand_id: str, **overrides) -> dict:
    body = {
        "name": "Q1 Growth Push",
        "brand_id": brand_id,
        "topic_cluster": "B2B SaaS onboarding friction",
        "platforms": ["LinkedIn"],
    }
    body.update(overrides)
    return body


async def test_create_and_list_campaign(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    # default workspace — X-Workspace-Id omitted, matches other test files' pattern

    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["name"] == "Q1 Growth Push"
    assert body["status"] == "draft"
    assert body["content_types"] == ["text"]
    assert body["piece_ids"] == []

    res = await api_client.get("/api/v1/campaigns/")
    assert res.status_code == 200
    names = [c["name"] for c in res.json()]
    assert "Q1 Growth Push" in names


async def test_create_campaign_404_for_nonexistent_brand(api_client):
    await signup_new_user(api_client)

    res = await api_client.post("/api/v1/campaigns/", json=_valid_body("does-not-exist"))
    assert res.status_code == 404


async def test_create_campaign_scrapes_article_url(api_client, monkeypatch):
    import app.api.v1.campaigns as campaigns_module

    async def fake_scrape_url(url: str) -> str:
        return "The full scraped article body, well past the 100-char minimum used elsewhere in this app."

    monkeypatch.setattr(campaigns_module, "scrape_url", fake_scrape_url)

    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)

    res = await api_client.post(
        "/api/v1/campaigns/",
        json=_valid_body(
            brand_id,
            source_type="article_url",
            topic_cluster="https://example.com/some-article",
        ),
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["source_type"] == "article_url"
    assert body["source_url"] == "https://example.com/some-article"
    assert body["topic_cluster"] == "The full scraped article body, well past the 100-char minimum used elsewhere in this app."


async def test_create_campaign_400_when_article_url_fails_to_scrape(api_client, monkeypatch):
    import app.api.v1.campaigns as campaigns_module

    async def fake_scrape_url(url: str) -> str:
        raise ValueError(f"Could not extract readable content from URL: {url}")

    monkeypatch.setattr(campaigns_module, "scrape_url", fake_scrape_url)

    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)

    res = await api_client.post(
        "/api/v1/campaigns/",
        json=_valid_body(brand_id, source_type="article_url", topic_cluster="https://example.com/paywalled"),
    )
    assert res.status_code == 400


async def test_create_campaign_rejects_unsupported_source_types(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)

    for source_type in ("youtube", "audio_upload", "podcast_rss"):
        res = await api_client.post(
            "/api/v1/campaigns/", json=_valid_body(brand_id, source_type=source_type),
        )
        assert res.status_code == 400, f"{source_type} should be rejected: {res.text}"


async def test_create_campaign_400_for_invalid_platform(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)

    res = await api_client.post(
        "/api/v1/campaigns/", json=_valid_body(brand_id, platforms=["NotARealPlatform"]),
    )
    assert res.status_code == 400


async def test_get_campaign_includes_progress(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    campaign_id = res.json()["id"]

    res = await api_client.get(f"/api/v1/campaigns/{campaign_id}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["progress"]["total"] == 0
    assert body["progress"]["drafting"] == 0


async def test_get_campaign_404_for_nonexistent(api_client):
    await signup_new_user(api_client)
    res = await api_client.get("/api/v1/campaigns/does-not-exist")
    assert res.status_code == 404


async def test_update_campaign(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    campaign_id = res.json()["id"]

    res = await api_client.patch(f"/api/v1/campaigns/{campaign_id}", json={"status": "paused"})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "paused"


async def test_update_campaign_400_when_nothing_provided(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    campaign_id = res.json()["id"]

    res = await api_client.patch(f"/api/v1/campaigns/{campaign_id}", json={})
    assert res.status_code == 400


async def test_delete_campaign_is_soft(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    campaign_id = res.json()["id"]

    res = await api_client.delete(f"/api/v1/campaigns/{campaign_id}")
    assert res.status_code == 204

    res = await api_client.get(f"/api/v1/campaigns/{campaign_id}")
    assert res.status_code == 404


async def test_campaigns_are_scoped_to_workspace(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    campaign_id = res.json()["id"]

    ws_id = await create_workspace(api_client, "Other Workspace", tier="duo")
    res = await api_client.get(f"/api/v1/campaigns/{campaign_id}", headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 404


async def test_generate_next_batch_tags_pieces_and_updates_progress(api_client, mock_llm):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post(
        "/api/v1/campaigns/", json=_valid_body(brand_id, platforms=["LinkedIn"]),
    )
    campaign_id = res.json()["id"]

    mock_llm.set_structured({"angles": ["Angle one", "Angle two"]})
    mock_llm.set_plain("Real generated content for this campaign day.")

    # days_per_batch defaults to 7, but batch_angles only returned 2 angles —
    # run_batch_pipeline's angles[:days] slice just runs what's available,
    # same fallback behaviour test_batch_mode.py documents.
    res = await api_client.post(f"/api/v1/campaigns/{campaign_id}/generate-next-batch")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["pieces_generated_this_run"] > 0
    assert body["status"] == "active"
    assert body["progress"]["total"] == body["pieces_generated_this_run"]

    # Every generated piece is real and tagged with this campaign.
    from app.db.mongo import content_pieces
    tagged = await content_pieces.count_documents({"campaign_id": campaign_id})
    assert tagged == body["pieces_generated_this_run"]


async def test_generate_next_batch_respects_days_per_batch(api_client, mock_llm):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post(
        "/api/v1/campaigns/",
        json=_valid_body(brand_id, platforms=["LinkedIn"], cadence={"frequency": "manual", "days_per_batch": 1}),
    )
    campaign_id = res.json()["id"]

    mock_llm.set_structured({"angles": ["Only angle"]})
    mock_llm.set_plain("Real generated content.")

    res = await api_client.post(f"/api/v1/campaigns/{campaign_id}/generate-next-batch")
    assert res.status_code == 200, res.text
    # 1 day * 1 platform = exactly 1 piece.
    assert res.json()["pieces_generated_this_run"] == 1


async def test_generate_next_batch_404_for_nonexistent_campaign(api_client, mock_llm):
    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/campaigns/does-not-exist/generate-next-batch")
    assert res.status_code == 404


async def test_generate_next_batch_400_when_brand_incomplete(api_client, mock_llm):
    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/brand/", json={"brand_type": "Person"})
    brand_id = res.json()["brand_profile_id"]  # never marked complete

    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    campaign_id = res.json()["id"]

    res = await api_client.post(f"/api/v1/campaigns/{campaign_id}/generate-next-batch")
    assert res.status_code == 400


async def test_create_campaign_requires_create_content_permission(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Campaign Perms", tier="large")
    brand_id = await _create_brand(api_client, ws_id)
    viewer_client, _ = await invite_and_accept(api_client, make_client, ws_id, "viewer")

    res = await viewer_client.post(
        "/api/v1/campaigns/", json=_valid_body(brand_id), headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403


async def test_generate_next_batch_requires_create_content_permission(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Campaign Perms", tier="large")
    brand_id = await _create_brand(api_client, ws_id)
    res = await api_client.post(
        "/api/v1/campaigns/", json=_valid_body(brand_id), headers={"X-Workspace-Id": ws_id},
    )
    campaign_id = res.json()["id"]

    viewer_client, _ = await invite_and_accept(api_client, make_client, ws_id, "viewer")
    res = await viewer_client.post(
        f"/api/v1/campaigns/{campaign_id}/generate-next-batch", headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# Recurring cadence (Phase 3)
# ─────────────────────────────────────────────────────────────────────────────

async def test_create_campaign_with_daily_cadence_sets_next_run_at(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)

    res = await api_client.post(
        "/api/v1/campaigns/",
        json=_valid_body(brand_id, cadence={"frequency": "daily", "days_per_batch": 3}),
    )
    assert res.status_code == 201, res.text
    assert res.json()["cadence"]["next_run_at"] is not None


async def test_create_campaign_manual_cadence_has_no_next_run_at(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)

    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    assert res.status_code == 201, res.text
    assert res.json()["cadence"]["next_run_at"] is None


async def test_update_campaign_cadence_to_manual_clears_next_run_at(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post(
        "/api/v1/campaigns/",
        json=_valid_body(brand_id, cadence={"frequency": "daily", "days_per_batch": 7}),
    )
    campaign_id = res.json()["id"]
    assert res.json()["cadence"]["next_run_at"] is not None

    res = await api_client.patch(
        f"/api/v1/campaigns/{campaign_id}",
        json={"cadence": {"frequency": "manual", "days_per_batch": 7}},
    )
    assert res.status_code == 200, res.text
    assert res.json()["cadence"]["next_run_at"] is None


async def test_generate_next_batch_advances_next_run_at_and_last_generated_at(api_client, mock_llm):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post(
        "/api/v1/campaigns/",
        json=_valid_body(brand_id, cadence={"frequency": "daily", "days_per_batch": 1}),
    )
    campaign_id = res.json()["id"]
    next_run_before = res.json()["cadence"]["next_run_at"]
    assert res.json()["last_generated_at"] is None

    mock_llm.set_structured({"angles": ["Only angle"]})
    mock_llm.set_plain("Real generated content.")

    res = await api_client.post(f"/api/v1/campaigns/{campaign_id}/generate-next-batch")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["last_generated_at"] is not None
    assert body["cadence"]["next_run_at"] is not None
    assert body["cadence"]["next_run_at"] > next_run_before


async def test_generate_next_batch_400_for_invalid_stored_platform(api_client, mock_llm):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    campaign_id = res.json()["id"]

    # Simulate stale/bad data bypassing create-time validation — direct
    # Mongo write, since the API itself never accepts an invalid platform.
    from app.db.mongo import get_campaigns_collection
    await get_campaigns_collection().update_one(
        {"id": campaign_id}, {"$set": {"platforms": ["NotARealPlatform"]}},
    )

    res = await api_client.post(f"/api/v1/campaigns/{campaign_id}/generate-next-batch")
    assert res.status_code == 400, res.text


# ─────────────────────────────────────────────────────────────────────────────
# Multi-platform / multi-day generation (Phase 2)
# ─────────────────────────────────────────────────────────────────────────────

async def test_create_campaign_accepts_platforms_by_day(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)

    res = await api_client.post(
        "/api/v1/campaigns/",
        json=_valid_body(
            brand_id,
            platforms=["LinkedIn"],
            platforms_by_day=[["LinkedIn"], ["LinkedIn", "Twitter/X"]],
            cadence={"frequency": "manual", "days_per_batch": 2},
        ),
    )
    assert res.status_code == 201, res.text
    assert res.json()["platforms_by_day"] == [["LinkedIn"], ["LinkedIn", "Twitter/X"]]


async def test_generate_next_batch_varies_platforms_per_day(api_client, mock_llm):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post(
        "/api/v1/campaigns/",
        json=_valid_body(
            brand_id,
            platforms=["LinkedIn"],
            platforms_by_day=[["LinkedIn"], ["LinkedIn", "Twitter/X"]],
            cadence={"frequency": "manual", "days_per_batch": 2},
        ),
    )
    campaign_id = res.json()["id"]

    mock_llm.set_structured({"angles": ["Day one angle", "Day two angle"]})
    mock_llm.set_plain("Real generated content.")

    res = await api_client.post(f"/api/v1/campaigns/{campaign_id}/generate-next-batch")
    assert res.status_code == 200, res.text
    assert res.json()["pieces_generated_this_run"] == 3  # day0: 1 platform, day1: 2 platforms

    from app.db.mongo import content_pieces
    day0 = await content_pieces.count_documents({"campaign_id": campaign_id, "batch_day_index": 0})
    day1 = await content_pieces.count_documents({"campaign_id": campaign_id, "batch_day_index": 1})
    assert day0 == 1
    assert day1 == 2


async def test_generate_next_batch_tags_pieces_with_batch_day_index_and_angle(api_client, mock_llm):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post(
        "/api/v1/campaigns/",
        json=_valid_body(brand_id, platforms=["LinkedIn"], cadence={"frequency": "manual", "days_per_batch": 2}),
    )
    campaign_id = res.json()["id"]

    mock_llm.set_structured({"angles": ["Angle A", "Angle B"]})
    mock_llm.set_plain("Real generated content.")

    res = await api_client.post(f"/api/v1/campaigns/{campaign_id}/generate-next-batch")
    assert res.status_code == 200, res.text

    from app.db.mongo import content_pieces
    pieces = await content_pieces.find({"campaign_id": campaign_id}).to_list(length=None)
    assert sorted({p["batch_day_index"] for p in pieces}) == [0, 1]
    assert all(p["angle"] for p in pieces)


# ─────────────────────────────────────────────────────────────────────────────
# Thumbnail upload — real Cloudinary call mocked out, never hits the network
# ─────────────────────────────────────────────────────────────────────────────

async def test_upload_campaign_thumbnail(api_client, monkeypatch):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    campaign_id = res.json()["id"]
    assert res.json()["thumbnail_url"] is None

    async def _fake_upload_file(file: bytes, content_type, user_id: str, filename=None) -> str:
        return "https://res.cloudinary.com/demo/image/upload/v1/recast/thumbnails/fake.png"

    import app.api.v1.campaigns as campaigns_module
    monkeypatch.setattr(campaigns_module, "upload_file", _fake_upload_file)

    res = await api_client.post(
        f"/api/v1/campaigns/{campaign_id}/thumbnail",
        files={"file": ("thumb.png", b"\x89PNG\r\n\x1a\n fake bytes", "image/png")},
    )
    assert res.status_code == 200, res.text
    assert res.json()["thumbnail_url"] == "https://res.cloudinary.com/demo/image/upload/v1/recast/thumbnails/fake.png"

    res2 = await api_client.get(f"/api/v1/campaigns/{campaign_id}")
    assert res2.json()["thumbnail_url"] == "https://res.cloudinary.com/demo/image/upload/v1/recast/thumbnails/fake.png"


async def test_upload_campaign_thumbnail_rejects_bad_content_type(api_client):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    campaign_id = res.json()["id"]

    res = await api_client.post(
        f"/api/v1/campaigns/{campaign_id}/thumbnail",
        files={"file": ("thumb.gif", b"GIF89a fake", "image/gif")},
    )
    assert res.status_code == 400
    assert "Unsupported image type" in res.json()["detail"]


async def test_upload_campaign_thumbnail_404_for_nonexistent_campaign(api_client):
    await signup_new_user(api_client)
    res = await api_client.post(
        "/api/v1/campaigns/does-not-exist/thumbnail",
        files={"file": ("thumb.png", b"fake", "image/png")},
    )
    assert res.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# Campaign topic AI suggestions
# ─────────────────────────────────────────────────────────────────────────────

async def test_suggest_topics_returns_llm_suggestions(api_client, mock_llm):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)

    mock_llm.set_structured({
        "suggested_topics": ["Angle one", "Angle two", "Angle three"],
        "suggested_tone": "Confident",
        "rationale": "These angles fit the brief.",
    })

    res = await api_client.post(
        "/api/v1/campaigns/suggest-topics",
        json={"topic_cluster": "B2B SaaS onboarding friction", "brand_id": brand_id, "existing_topics": []},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["suggested_topics"] == ["Angle one", "Angle two", "Angle three"]
    assert body["suggested_tone"] == "Confident"


async def test_suggest_topics_falls_back_gracefully_when_llm_returns_nothing(api_client, mock_llm):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    mock_llm.set_structured({})

    res = await api_client.post(
        "/api/v1/campaigns/suggest-topics",
        json={"topic_cluster": "B2B SaaS onboarding friction", "brand_id": brand_id},
    )
    assert res.status_code == 200, res.text
    assert res.json()["suggested_topics"] == []


async def test_suggest_topics_404_for_nonexistent_brand(api_client):
    await signup_new_user(api_client)
    res = await api_client.post(
        "/api/v1/campaigns/suggest-topics",
        json={"topic_cluster": "Some brief", "brand_id": "does-not-exist"},
    )
    assert res.status_code == 404


async def test_campaign_export_is_a_zip_with_the_docx_and_each_posts_media(api_client, monkeypatch):
    import io
    import zipfile

    import app.api.v1.content as content_module
    from app.db.mongo import content_pieces

    sfx = __import__('uuid').uuid4().hex[:8]
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))
    campaign = res.json()
    ws_id = campaign["workspace_id"]
    base = {
        "workspace_id": ws_id, "campaign_id": campaign["id"], "platform": "Instagram",
        "created_at": __import__("datetime").datetime(2026, 10, 3), "deleted": False,
    }
    await content_pieces.insert_one({
        **base, "id": sfx + "-1", "piece_id": sfx + "-1", "content": "Launch day\nBig news",
        "media": [{"url": "https://files.test/a.png", "mime_type": "image/png", "kind": "image"}],
    })
    # a piece from another campaign must not appear
    await content_pieces.insert_one({**base, "id": sfx + "-2", "piece_id": sfx + "-2", "campaign_id": "other", "content": "Not mine"})

    async def fake_fetch(url, client, limit):
        return b"PNG"

    monkeypatch.setattr(content_module, "_fetch_media", fake_fetch)
    res = await api_client.get(f"/api/v1/campaigns/{campaign['id']}/export")
    assert res.status_code == 200, res.text
    zf = zipfile.ZipFile(io.BytesIO(res.content))
    assert "q1-growth-push.docx" in zf.namelist()
    assert "media/launch-day_2026-10-03_instagram/image-1.png" in zf.namelist()
    from docx import Document

    text = " ".join(p.text for p in Document(io.BytesIO(zf.read("q1-growth-push.docx"))).paragraphs)
    assert "Big news" in text and "Not mine" not in text


async def test_campaign_export_of_an_unknown_campaign_is_404(api_client):
    await signup_new_user(api_client)
    res = await api_client.get("/api/v1/campaigns/nope/export")
    assert res.status_code == 404


async def _campaign_with_media(api_client, mock_llm, **media):
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    res = await api_client.post(
        "/api/v1/campaigns/",
        json=_valid_body(brand_id, cadence={"frequency": "manual", "days_per_batch": 1}, media_plan=media),
    )
    assert res.status_code == 201, res.text
    mock_llm.set_structured({"angles": ["Only angle"]})
    mock_llm.set_plain("Real generated content.")
    return res.json()


async def test_a_campaign_without_media_asks_the_media_step_for_nothing(api_client, mock_llm, monkeypatch):
    from app.pipelines.campaigns import media as campaign_media

    calls = []

    async def spy(*a, **k):
        calls.append(1)
        return {}

    monkeypatch.setattr(campaign_media, "generate_media_for_pieces", spy)
    campaign = await _campaign_with_media(api_client, mock_llm)
    assert campaign["media_plan"]["enabled"] is False and campaign["content_types"] == ["text"]
    res = await api_client.post(f"/api/v1/campaigns/{campaign['id']}/generate-next-batch")
    assert res.status_code == 200 and calls == []


async def test_media_runs_for_each_saved_post_when_the_campaign_asks_for_it(api_client, mock_llm, monkeypatch):
    from app.pipelines.campaigns import media as campaign_media

    seen = []

    async def fake(campaign, piece_ids, **k):
        seen.append(list(piece_ids))
        return {"images": 1, "audio": 1, "failed": 0}

    monkeypatch.setattr(campaign_media, "generate_media_for_pieces", fake)
    campaign = await _campaign_with_media(api_client, mock_llm, enabled=True, kinds=["image", "audio"])
    assert campaign["content_types"] == ["text", "audio", "image"]
    res = await api_client.post(f"/api/v1/campaigns/{campaign['id']}/generate-next-batch")
    assert res.status_code == 200, res.text
    assert len(seen) == 1 and len(seen[0]) == res.json()["pieces_generated_this_run"]


async def test_a_media_failure_never_loses_the_text(api_client, mock_llm, monkeypatch):
    from app.pipelines.campaigns import media as campaign_media

    async def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(campaign_media, "generate_media_for_pieces", boom)
    campaign = await _campaign_with_media(api_client, mock_llm, enabled=True, kinds=["audio"])
    res = await api_client.post(f"/api/v1/campaigns/{campaign['id']}/generate-next-batch")
    assert res.status_code == 200, res.text
    assert res.json()["pieces_generated_this_run"] == 1


async def test_media_plan_can_be_changed_after_creation(api_client, mock_llm):
    campaign = await _campaign_with_media(api_client, mock_llm)
    res = await api_client.patch(
        f"/api/v1/campaigns/{campaign['id']}",
        json={"media_plan": {"enabled": True, "kinds": ["image"], "count_per_post": 3, "image": {"layout": "story_9_16"}}},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["content_types"] == ["text", "image"] and body["media_plan"]["count_per_post"] == 3


async def test_campaign_media_lists_each_posts_audio_and_images(api_client):
    sfx = __import__("uuid").uuid4().hex[:8]
    PID_MEDIA = "cp-media-" + sfx
    ma, mi = "m-a-" + sfx, "m-i-" + sfx
    au1, im1 = "au1-" + sfx, "im1-" + sfx
    from datetime import datetime

    from app.db.mongo import audio_assets, content_pieces, image_assets, media_assets

    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    campaign = (await api_client.post("/api/v1/campaigns/", json=_valid_body(brand_id))).json()
    ws = campaign["workspace_id"]
    now = datetime(2026, 10, 3)
    await content_pieces.insert_one({
        "id": PID_MEDIA, "piece_id": PID_MEDIA, "workspace_id": ws, "campaign_id": campaign["id"],
        "platform": "LinkedIn", "content": "Hello", "created_at": now, "deleted": False,
    })
    for mid, url, mime in ((ma, "https://f/a.mp3", "audio/mpeg"), (mi, "https://f/i.png", "image/png")):
        await media_assets.insert_one({"id": mid, "workspace_id": ws, "url": url, "mime_type": mime})
    await audio_assets.insert_one({"id": au1, "workspace_id": ws, "source_piece_id": PID_MEDIA, "media_id": ma, "title": "Hello"})
    await image_assets.insert_one({
        "id": im1, "workspace_id": ws, "source_piece_id": PID_MEDIA, "title": "Hello",
        "slides": [{"slide_number": 1, "media_id": mi}],
    })
    await audio_assets.insert_one({"id": "au2-" + sfx, "workspace_id": "other", "source_piece_id": PID_MEDIA, "media_id": ma})

    res = await api_client.get(f"/api/v1/campaigns/{campaign['id']}/media")
    assert res.status_code == 200, res.text
    kinds = sorted(m["kind"] for m in res.json()["items"][PID_MEDIA])
    assert kinds == ["audio", "image"]
    assert (await api_client.get("/api/v1/campaigns/nope/media")).status_code == 404


async def _post_with_failed_media(api_client, status):
    from datetime import datetime

    from app.db.mongo import content_pieces

    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)
    campaign = (await api_client.post(
        "/api/v1/campaigns/", json=_valid_body(brand_id, media_plan={"enabled": True, "kinds": ["image", "audio"]}),
    )).json()
    pid = "cp-retry-" + __import__("uuid").uuid4().hex[:8]
    await content_pieces.insert_one({
        "id": pid, "piece_id": pid, "workspace_id": campaign["workspace_id"], "campaign_id": campaign["id"],
        "platform": "LinkedIn", "content": "Hello there", "created_at": datetime(2026, 10, 3), "deleted": False,
        "media_status": status,
    })
    campaign["_pid"] = pid
    return campaign


async def test_media_status_is_reported_per_post(api_client):
    campaign = await _post_with_failed_media(api_client, {"image": "ready", "audio": "failed"})
    res = await api_client.get(f"/api/v1/campaigns/{campaign['id']}/media")
    assert res.json()["status"] == {campaign["_pid"]: {"image": "ready", "audio": "failed"}}


async def test_retry_makes_again_only_what_failed(api_client, monkeypatch):
    from app.pipelines.campaigns import media as campaign_media

    tried = []

    async def fake_make(campaign, piece, kinds, ctx):
        tried.append(list(kinds))
        return {k: "ready" for k in kinds}

    monkeypatch.setattr(campaign_media, "_make_for_piece", fake_make)
    campaign = await _post_with_failed_media(api_client, {"image": "ready", "audio": "failed"})
    res = await api_client.post(f"/api/v1/campaigns/{campaign['id']}/pieces/{campaign['_pid']}/retry-media")
    assert res.status_code == 200, res.text
    assert tried == [["audio"]] and res.json()["status"] == {"audio": "ready"}
    after = await api_client.get(f"/api/v1/campaigns/{campaign['id']}/media")
    assert after.json()["status"][campaign["_pid"]] == {"image": "ready", "audio": "ready"}


async def test_retry_with_nothing_failed_does_nothing(api_client, monkeypatch):
    from app.pipelines.campaigns import media as campaign_media

    async def boom(*a, **k):
        raise AssertionError("must not generate")

    monkeypatch.setattr(campaign_media, "_make_for_piece", boom)
    campaign = await _post_with_failed_media(api_client, {"image": "ready"})
    res = await api_client.post(f"/api/v1/campaigns/{campaign['id']}/pieces/{campaign['_pid']}/retry-media")
    assert res.status_code == 200 and res.json()["status"] == {}


async def test_retry_for_an_unknown_campaign_is_404(api_client):
    await signup_new_user(api_client)
    res = await api_client.post("/api/v1/campaigns/nope/pieces/x/retry-media")
    assert res.status_code == 404
