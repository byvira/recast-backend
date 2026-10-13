"""Where a connected account's posts go: the connected profile by default, a Company Page only once LinkedIn has approved the app."""
import pytest

from app.core.config import settings
from app.db.mongo import platform_ops, workspace_connections
from app.pipelines.publish import executor
from app.pipelines.publish.destinations_support import destination_enabled, effective, kinds_for
from app.pipelines.publish.base import PublishRequest
from app.pipelines.publish.linkedin import oauth as linkedin_oauth
from app.pipelines.publish.linkedin.posts_api import author_urn
from app.pipelines.publish.token_store import get_all_tokens, get_token, save_token
from tests.test_image_assets import _h, _setup


async def _connect(ws_id: str, user_id: str = "abc123", name: str = "Sam Suggest") -> None:
    await save_token(ws_id, "linkedin", access_token="tok", refresh_token=None, expires_at=None, platform_user_id=user_id, username=name)


@pytest.fixture
def organizations(monkeypatch):
    found = [{"id": "42", "name": "Acme Co"}, {"id": "77", "name": "Acme Labs"}]

    async def fake(access_token):
        return list(found)

    monkeypatch.setattr(linkedin_oauth, "list_admin_organizations", fake)
    return found


def test_the_author_is_the_profile_unless_a_company_page_is_set():
    assert author_urn(PublishRequest(piece_id="p", user_id="u", brand_id="b", platform="linkedin", content="x", platform_user_id="abc")) == "urn:li:person:abc"
    assert author_urn(PublishRequest(piece_id="p", user_id="u", brand_id="b", platform="linkedin", content="x", platform_user_id="abc", author_urn="urn:li:organization:42")) == "urn:li:organization:42"


def test_company_page_permissions_are_asked_for_only_when_asked():
    assert "w_organization_social" not in linkedin_oauth.scopes() and "w_member_social" in linkedin_oauth.scopes()
    assert {"w_organization_social", "r_organization_admin", "w_member_social"} <= set(linkedin_oauth.scopes(include_org=True))
    assert "w_organization_social" in linkedin_oauth.build_auth_url("state", include_org=True)
    assert "w_organization_social" not in linkedin_oauth.build_auth_url("state")


def test_only_platforms_with_a_real_choice_have_destinations():
    assert [k.kind for k in kinds_for("linkedin")] == ["organization"]
    assert [k.kind for k in kinds_for("facebook")] == ["page"]
    for single_account in ("twitter", "threads", "bluesky", "instagram", "youtube", "google"):
        assert kinds_for(single_account) == [], single_account


def test_a_choice_is_open_by_ops_setting_else_by_its_default(monkeypatch):
    monkeypatch.setattr(settings, "LINKEDIN_ORG_POSTING_ENABLED", False)
    assert effective({}, "linkedin", "organization") is False  # needs LinkedIn's approval, so it starts closed
    assert effective({"destinations": {"organization": True}}, "linkedin", "organization") is True
    assert effective({"destinations": {"organization": False}}, "linkedin", "organization") is False
    monkeypatch.setattr(settings, "LINKEDIN_ORG_POSTING_ENABLED", True)
    assert effective({}, "linkedin", "organization") is True and effective({"destinations": {"organization": False}}, "linkedin", "organization") is False
    assert effective({"destinations": {"page": False}}, "facebook", "page") is True  # not switchable
    assert effective({}, "twitter", "organization") is False  # a platform with no such choice


async def test_by_default_posts_go_to_the_connected_profile_and_company_pages_wait_for_approval(signup_user, monkeypatch):
    monkeypatch.setattr(settings, "LINKEDIN_ORG_POSTING_ENABLED", False)
    client, _, ws_id, _ = await _setup(signup_user)
    await _connect(ws_id)

    res = await client.get("/api/v1/post-targets/linkedin", headers=_h(ws_id))
    assert res.status_code == 200, res.text
    profile, page = res.json()["options"]
    assert profile["kind"] == "profile" and profile["selected"] is True and profile["available"] is True
    assert page["kind"] == "organization" and page["available"] is False and "LinkedIn approves" in page["reason"]

    refused = await client.put("/api/v1/post-targets/linkedin", json={"kind": "organization", "id": "42"}, headers=_h(ws_id))
    assert refused.status_code == 409 and "LinkedIn approves" in refused.json()["detail"]
    # choosing the profile is always allowed
    assert (await client.put("/api/v1/post-targets/linkedin", json={"kind": "profile"}, headers=_h(ws_id))).status_code == 200


async def test_with_approval_a_company_page_can_be_chosen_and_is_what_gets_published_as(signup_user, monkeypatch, organizations):
    monkeypatch.setattr(settings, "LINKEDIN_ORG_POSTING_ENABLED", True)
    client, _, ws_id, _ = await _setup(signup_user)
    await _connect(ws_id)

    listed = (await client.get("/api/v1/post-targets/linkedin", headers=_h(ws_id))).json()["options"]
    assert [o["name"] for o in listed if o["kind"] == "organization"] == ["Acme Co", "Acme Labs"]
    assert next(o for o in listed if o["kind"] == "profile")["selected"] is True

    res = await client.put("/api/v1/post-targets/linkedin", json={"kind": "organization", "id": "42"}, headers=_h(ws_id))
    assert res.status_code == 200, res.text
    assert [o["name"] for o in res.json()["options"] if o["selected"]] == ["Acme Co"]

    token = await get_token(ws_id, "linkedin")
    assert token["post_target"]["urn"] == "urn:li:organization:42"
    summary = next(a for a in await get_all_tokens(ws_id) if a["platform"] == "linkedin")
    assert summary["post_target"] == {"kind": "organization", "name": "Acme Co"} and "urn" not in summary["post_target"]

    async def no_media(piece, workspace_id, platform):
        return []

    monkeypatch.setattr(executor, "media_for_publish", no_media)
    piece = {"piece_id": "p1", "brand_id": "b1", "content": "Hello"}
    request = await executor.build_request(piece, ws_id, "u", "linkedin", token)
    assert author_urn(request) == "urn:li:organization:42"

    # a Page this sign-in does not manage is refused, and the profile can always be chosen again
    unknown = await client.put("/api/v1/post-targets/linkedin", json={"kind": "organization", "id": "999"}, headers=_h(ws_id))
    assert unknown.status_code == 404
    back = await client.put("/api/v1/post-targets/linkedin", json={"kind": "profile"}, headers=_h(ws_id))
    assert next(o for o in back.json()["options"] if o["kind"] == "profile")["selected"] is True
    request = await executor.build_request(piece, ws_id, "u", "linkedin", await get_token(ws_id, "linkedin"))
    assert author_urn(request) == "urn:li:person:abc123"


async def test_a_saved_company_page_is_ignored_while_the_switch_is_off(signup_user, monkeypatch, organizations):
    client, _, ws_id, _ = await _setup(signup_user)
    await _connect(ws_id)
    await workspace_connections.update_one(
        {"workspace_id": ws_id, "platform": "linkedin"}, {"$set": {"post_target": {"kind": "organization", "id": "42", "name": "Acme Co", "urn": "urn:li:organization:42"}}},
    )

    async def no_media(piece, workspace_id, platform):
        return []

    monkeypatch.setattr(executor, "media_for_publish", no_media)
    monkeypatch.setattr(settings, "LINKEDIN_ORG_POSTING_ENABLED", False)
    request = await executor.build_request({"piece_id": "p", "brand_id": "b", "content": "x"}, ws_id, "u", "linkedin", await get_token(ws_id, "linkedin"))
    assert author_urn(request) == "urn:li:person:abc123", "without approval the post must never be sent as a Company Page"


async def test_signing_in_as_someone_else_clears_the_choice_but_renewing_keeps_it(signup_user):
    client, _, ws_id, _ = await _setup(signup_user)
    await _connect(ws_id, user_id="abc123")
    target = {"kind": "organization", "id": "42", "name": "Acme Co", "urn": "urn:li:organization:42"}
    await workspace_connections.update_one({"workspace_id": ws_id, "platform": "linkedin"}, {"$set": {"post_target": target}})

    await _connect(ws_id, user_id="abc123")  # a renewal of the same account
    assert (await get_token(ws_id, "linkedin"))["post_target"] == target
    await _connect(ws_id, user_id="someone-else")  # a different LinkedIn account
    assert (await get_token(ws_id, "linkedin"))["post_target"] is None


async def test_an_unconnected_account_and_other_platforms_are_refused(signup_user):
    client, _, ws_id, _ = await _setup(signup_user)
    assert (await client.get("/api/v1/post-targets/linkedin", headers=_h(ws_id))).status_code == 404
    assert (await client.get("/api/v1/post-targets/bluesky", headers=_h(ws_id))).status_code == 422


# ── Ops controls each platform's choices ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
async def clean_ops():
    await platform_ops.delete_many({"platform_key": {"$in": ["linkedin", "facebook"]}})
    yield
    await platform_ops.delete_many({"platform_key": {"$in": ["linkedin", "facebook"]}})


async def test_ops_lists_and_switches_a_platforms_destinations(make_client, clean_ops, monkeypatch):
    from tests.test_platform_ops import _staff

    monkeypatch.setattr(settings, "LINKEDIN_ORG_POSTING_ENABLED", False)
    owner, _ = await _staff(make_client, master=True)
    got = (await owner.get("/api/v1/ops/platforms/linkedin/destinations")).json()
    row = got["destinations"][0]
    assert row["kind"] == "organization" and row["enabled"] is False and row["needs_approval"] is True and row["switchable"] is True

    res = await owner.put("/api/v1/ops/platforms/linkedin/destinations", json={"destinations": {"organization": True}, "version": got["version"]})
    assert res.status_code == 200, res.text
    assert res.json()["destinations"][0]["enabled"] is True
    # the same change made on a stale copy is refused
    stale = await owner.put("/api/v1/ops/platforms/linkedin/destinations", json={"destinations": {"organization": False}, "version": got["version"]})
    assert stale.status_code == 409
    # and now members see Company Pages as open, without any change to the environment
    assert await destination_enabled("linkedin", "organization") is True


async def test_ops_cannot_switch_what_does_not_exist_or_is_always_on(make_client, clean_ops):
    from tests.test_platform_ops import _staff

    owner, _ = await _staff(make_client, master=True)
    version = (await owner.get("/api/v1/ops/platforms/facebook/destinations")).json()["version"]
    always_on = await owner.put("/api/v1/ops/platforms/facebook/destinations", json={"destinations": {"page": False}, "version": version})
    assert always_on.status_code == 400 and always_on.json()["detail"]["code"] == "not_switchable"
    unknown = await owner.put("/api/v1/ops/platforms/linkedin/destinations", json={"destinations": {"group": True}, "version": 0})
    assert unknown.status_code == 400 and unknown.json()["detail"]["code"] == "unknown_destination"
    single = (await owner.get("/api/v1/ops/platforms/twitter/destinations")).json()
    assert single["destinations"] == []


async def test_only_the_ops_owner_can_change_destinations(make_client, clean_ops):
    from tests.test_platform_ops import _staff

    staff, _ = await _staff(make_client, master=False)
    assert (await staff.get("/api/v1/ops/platforms/linkedin/destinations")).status_code == 200
    denied = await staff.put("/api/v1/ops/platforms/linkedin/destinations", json={"destinations": {"organization": True}, "version": 0})
    assert denied.status_code == 403


async def test_members_get_company_pages_once_ops_opens_them(signup_user, make_client, clean_ops, monkeypatch, organizations):
    from tests.test_platform_ops import _staff

    monkeypatch.setattr(settings, "LINKEDIN_ORG_POSTING_ENABLED", False)
    client, _, ws_id, _ = await _setup(signup_user)
    await _connect(ws_id)
    before = (await client.get("/api/v1/post-targets/linkedin", headers=_h(ws_id))).json()["options"]
    assert not any(o["kind"] == "organization" and o["available"] for o in before)

    owner, _ = await _staff(make_client, master=True)
    version = (await owner.get("/api/v1/ops/platforms/linkedin/destinations")).json()["version"]
    await owner.put("/api/v1/ops/platforms/linkedin/destinations", json={"destinations": {"organization": True}, "version": version})

    after = (await client.get("/api/v1/post-targets/linkedin", headers=_h(ws_id))).json()["options"]
    assert [o["name"] for o in after if o["kind"] == "organization" and o["available"]] == ["Acme Co", "Acme Labs"]
