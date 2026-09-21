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
