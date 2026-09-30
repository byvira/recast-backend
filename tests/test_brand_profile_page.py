"""Brand profile page routes: field config, per-section save with a version check, counts,
duplicate, word-list validation, completeness. Real database; nothing external is called."""

from datetime import datetime, timezone
from uuid import uuid4

from tests.conftest import create_workspace, invite_and_accept, signup_new_user


async def _brand(client, ws_id=None, brand_type="Person") -> str:
    headers = {"X-Workspace-Id": ws_id} if ws_id else {}
    res = await client.post("/api/v1/brand/", json={"brand_type": brand_type}, headers=headers)
    assert res.status_code in (200, 201), res.text
    return res.json()["brand_profile_id"]


async def _get(client, brand_id, ws_id=None):
    res = await client.get(f"/api/v1/brand/{brand_id}", headers={"X-Workspace-Id": ws_id} if ws_id else {})
    assert res.status_code == 200, res.text
    return res.json()


async def test_identity_fields_are_served_for_every_type(api_client):
    await signup_new_user(api_client)
    res = await api_client.get("/api/v1/brand/identity-fields")
    assert res.status_code == 200
    body = res.json()
    assert set(body["types"]) == {"Person", "Personal Brand", "Business", "Product", "Shop", "Entertainment"}
    assert [f["key"] for f in body["types"]["Business"]][0] == "company_name"
    assert body["limits"]["banned_words"] == 50


async def test_a_profile_reports_its_completeness_and_a_version(api_client):
    await signup_new_user(api_client)
    brand = await _get(api_client, await _brand(api_client))
    assert brand["completeness"]["percent"] == 0 and brand["completeness"]["first_incomplete"] == "identity"
    assert brand["version"]


async def test_identity_is_saved_cleaned_and_keeps_fields_stored_elsewhere(api_client):
    from app.db.mongo import brand_profiles

    await signup_new_user(api_client)
    brand_id = await _brand(api_client)
    await brand_profiles.update_one({"id": brand_id}, {"$set": {"identity": {"name": "Asha", "legacy": "keep"}}})
    res = await api_client.patch(
        f"/api/v1/brand/{brand_id}/sections/identity",
        json={"data": {"name": "  Asha   Rao ", "bio": "Coach", "website": "asha.dev", "achievements": ["A", "a", "B"]}},
    )
    assert res.status_code == 200, res.text
    identity = res.json()["identity"]
    assert identity["name"] == "Asha Rao" and identity["website"] == "https://asha.dev"
    assert identity["achievements"] == ["A", "B"] and identity["legacy"] == "keep"


async def test_bad_identity_is_refused_with_the_fields_named(api_client):
    await signup_new_user(api_client)
    brand_id = await _brand(api_client)
    res = await api_client.patch(
        f"/api/v1/brand/{brand_id}/sections/identity", json={"data": {"name": "", "website": "not a url"}},
    )
    assert res.status_code == 422
    assert set(res.json()["detail"]["errors"]) == {"name", "website"}


async def test_a_stale_version_is_refused_and_the_current_one_is_accepted(api_client):
    await signup_new_user(api_client)
    brand_id = await _brand(api_client)
    first = await _get(api_client, brand_id)
    ok = await api_client.patch(
        f"/api/v1/brand/{brand_id}/sections/identity", json={"data": {"name": "One"}},
        headers={"X-Brand-Version": first["version"]},
    )
    assert ok.status_code == 200, ok.text
    stale = await api_client.patch(
        f"/api/v1/brand/{brand_id}/sections/identity", json={"data": {"name": "Two"}},
        headers={"X-Brand-Version": first["version"]},
    )
    assert stale.status_code == 409 and "changed" in stale.json()["detail"]
    fresh = await api_client.patch(
        f"/api/v1/brand/{brand_id}/sections/identity", json={"data": {"name": "Three"}},
        headers={"X-Brand-Version": ok.json()["version"]},
    )
    assert fresh.status_code == 200 and fresh.json()["identity"]["name"] == "Three"


async def test_audience_platforms_and_onboarding_answers(api_client):
    await signup_new_user(api_client)
    brand_id = await _brand(api_client, brand_type="Business")
    aud = await api_client.patch(
        f"/api/v1/brand/{brand_id}/sections/audience",
        json={"data": {"reading_level": "Expert", "knowledge_base": "Advanced", "primary_pain_point": "No time"}},
    )
    assert aud.status_code == 200 and aud.json()["audience"]["primary_pain_point"] == "No time"
    bad = await api_client.patch(f"/api/v1/brand/{brand_id}/sections/audience", json={"data": {"reading_level": "Genius"}})
    assert bad.status_code == 422
    plats = await api_client.patch(f"/api/v1/brand/{brand_id}/sections/platforms", json={"data": {"platforms": ["linkedin", "instagram"]}})
    assert plats.json()["platforms"] == ["linkedin", "instagram"]
    ans = await api_client.patch(f"/api/v1/brand/{brand_id}/sections/onboarding_answers", json={"data": {"segments": ["SMB"]}})
    assert ans.status_code == 200 and ans.json()["icp_data"] == {"segments": ["SMB"]}


async def test_a_person_has_no_extra_onboarding_answers_and_an_unknown_section_is_404(api_client):
    await signup_new_user(api_client)
    brand_id = await _brand(api_client)
    assert (await api_client.patch(f"/api/v1/brand/{brand_id}/sections/onboarding_answers", json={"data": {"a": 1}})).status_code == 400
    assert (await api_client.patch(f"/api/v1/brand/{brand_id}/sections/nonsense", json={"data": {}})).status_code == 404


async def test_saving_a_section_does_not_move_the_onboarding_progress(api_client):
    await signup_new_user(api_client)
    brand_id = await _brand(api_client)
    before = await _get(api_client, brand_id)
    await api_client.patch(f"/api/v1/brand/{brand_id}/sections/identity", json={"data": {"name": "X"}})
    after = await _get(api_client, brand_id)
    assert after["onboarding_step"] == before["onboarding_step"]


async def test_a_banned_word_cannot_also_be_a_replacement(api_client):
    await signup_new_user(api_client)
    brand_id = await _brand(api_client)
    bad = await api_client.patch(f"/api/v1/brand/{brand_id}/voice", json={"manual_data": {
        "banned_words": ["leverage"], "preferred_synonyms": [{"original": "use", "replacement": "Leverage"}],
    }})
    assert bad.status_code == 422 and "preferred_synonyms" in bad.json()["detail"]["errors"]
    ok = await api_client.patch(f"/api/v1/brand/{brand_id}/voice", json={"manual_data": {
        "banned_words": ["Synergy", "synergy"], "openers": ["Hi"],
    }})
    assert ok.status_code == 200 and ok.json()["manual_data"]["banned_words"] == ["Synergy"]


async def test_the_voice_route_also_honours_the_version(api_client):
    await signup_new_user(api_client)
    brand_id = await _brand(api_client)
    first = await _get(api_client, brand_id)
    await api_client.patch(f"/api/v1/brand/{brand_id}/sections/identity", json={"data": {"name": "Moved on"}})
    stale = await api_client.patch(
        f"/api/v1/brand/{brand_id}/voice", json={"default_tone": "brand"}, headers={"X-Brand-Version": first["version"]},
    )
    assert stale.status_code == 409


async def test_counts_cover_posts_images_audio_campaigns_and_presets(api_client):
    from app.db.mongo import audio_assets, content_pieces, get_campaigns_collection, image_assets, presets

    await signup_new_user(api_client)
    brand_id = await _brand(api_client)
    ws = (await _get(api_client, brand_id))["workspace_id"]
    sfx = uuid4().hex[:8]
    now = datetime(2026, 10, 3, tzinfo=timezone.utc)
    await content_pieces.insert_one({"id": sfx + "p1", "piece_id": sfx + "p1", "workspace_id": ws, "brand_id": brand_id,
                                     "content": "a", "created_at": now, "deleted": False, "publish_status": "published"})
    await content_pieces.insert_one({"id": sfx + "p2", "piece_id": sfx + "p2", "workspace_id": ws, "brand_id": brand_id,
                                     "content": "b", "created_at": now, "deleted": False})
    await content_pieces.insert_one({"id": sfx + "p3", "piece_id": sfx + "p3", "workspace_id": ws, "brand_id": "other",
                                     "content": "c", "created_at": now, "deleted": False})
    await image_assets.insert_one({"id": "i" + sfx, "workspace_id": ws, "brand_id": brand_id})
    await audio_assets.insert_one({"id": "a" + sfx, "workspace_id": ws, "brand_id": brand_id})
    await audio_assets.insert_one({"id": "b" + sfx, "workspace_id": ws, "brand_id": brand_id})
    await get_campaigns_collection().insert_one({"id": "c" + sfx, "workspace_id": ws, "brand_id": brand_id, "deleted": False})
    await presets.insert_one({"id": "pr" + sfx, "workspace_id": ws, "voice_binding_id": brand_id, "deleted": False})

    res = await api_client.get(f"/api/v1/brand/{brand_id}/counts")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["posts"]["total"] == 2 and body["posts"]["by_stage"]["published"] == 1
    assert (body["images"], body["audio"], body["campaigns"], body["presets"]) == (1, 2, 1, 1)
    assert (await api_client.get("/api/v1/brand/nope/counts")).status_code == 404


async def test_duplicate_makes_a_non_default_copy_named_copy(api_client):
    await signup_new_user(api_client)
    brand_id = await _brand(api_client)
    await api_client.patch(f"/api/v1/brand/{brand_id}/sections/identity", json={"data": {"name": "Asha"}})
    await api_client.patch(f"/api/v1/brand/{brand_id}/set-default")
    res = await api_client.post(f"/api/v1/brand/{brand_id}/duplicate")
    assert res.status_code == 201, res.text
    copy = await _get(api_client, res.json()["brand_profile_id"])
    assert copy["identity"]["name"] == "Asha (copy)" and copy["is_default"] is False and copy["id"] != brand_id


async def test_viewers_can_read_but_not_edit_the_profile(api_client, make_client):
    await signup_new_user(api_client)
    ws_id = await create_workspace(api_client, "Profile Perms", tier="large")
    brand_id = await _brand(api_client, ws_id)
    viewer, _ = await invite_and_accept(api_client, make_client, ws_id, "viewer")
    headers = {"X-Workspace-Id": ws_id}
    assert (await viewer.get(f"/api/v1/brand/{brand_id}", headers=headers)).status_code == 200
    assert (await viewer.get(f"/api/v1/brand/{brand_id}/counts", headers=headers)).status_code == 200
    assert (await viewer.patch(f"/api/v1/brand/{brand_id}/sections/identity", json={"data": {"name": "X"}}, headers=headers)).status_code == 403
    assert (await viewer.post(f"/api/v1/brand/{brand_id}/duplicate", headers=headers)).status_code == 403


async def test_the_default_platform_follows_the_platform_list(api_client):
    await signup_new_user(api_client)
    brand_id = await _brand(api_client)
    url = f"/api/v1/brand/{brand_id}/sections/platforms"
    first = await api_client.patch(url, json={"data": {"platforms": ["LinkedIn", "Instagram"]}})
    assert first.json()["default_platform"] == "LinkedIn"
    chosen = await api_client.patch(url, json={"data": {"platforms": ["LinkedIn", "Instagram"], "default_platform": "Instagram"}})
    assert chosen.json()["default_platform"] == "Instagram"
    removed = await api_client.patch(url, json={"data": {"platforms": ["LinkedIn"]}})
    assert removed.json()["default_platform"] == "LinkedIn"
    cleared = await api_client.patch(url, json={"data": {"platforms": []}})
    assert cleared.json()["default_platform"] is None
