"""Tests for /api/v1/image-assets — the real Image pipeline (all 9 layouts,
carousel CRUD, governance, export). Previously ~20 endpoints with no
automated regression coverage at all.

Real Mongo, real Pillow rendering (bundled fonts, no network). Only the two
external effects are stubbed, exactly like the rest of this suite never calls
a real provider: the AI background (Cloudflare/Gemini) and the Cloudinary
upload.
"""

import io
from uuid import uuid4

import pytest
from PIL import Image

from app.api.v1 import image_assets as image_module
from app.db.mongo import brand_profiles, image_asset_versions, media_assets
from app.models.image_asset import LayoutPreset
from app.pipelines.media.image_render import LAYOUT_DIMS
from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from tests.conftest import create_workspace, invite_and_accept

_CLOUDINARY_URL = "https://res.cloudinary.com/demo/image/upload/v1/{name}.png"


def _png(size: tuple[int, int] = (64, 64), color=(30, 60, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def stubs(monkeypatch):
    """Stubs the AI background + the upload. Records every call so tests can
    assert on what was really produced (the uploaded bytes are the real
    Pillow render)."""
    record = {"backgrounds": [], "uploads": [], "vision_calls": 0, "vision_reply": "A blue square on a plain background."}

    async def _fake_background(*, prompt, workspace_id, user_id, target_size, brand_profile, avoid=None):
        record["backgrounds"].append({"prompt": prompt, "size": target_size})
        return _png(target_size)

    async def _fake_upload(data, content_type, user_id):
        record["uploads"].append(data)
        return _CLOUDINARY_URL.format(name=uuid4().hex)

    async def _fake_vision(prompt, image_bytes, mime_type="image/jpeg", *args, **kwargs):
        # Never a real Gemini call: describing an uploaded image is best-effort
        # and would otherwise spend real quota on every upload test.
        record["vision_calls"] += 1
        return record["vision_reply"]

    monkeypatch.setattr(image_module, "generate_image_from_prompt", _fake_background)
    monkeypatch.setattr(image_module, "upload_file", _fake_upload)
    monkeypatch.setattr(image_module, "call_vision", _fake_vision)
    return record


async def _brand(client, ws_id: str) -> str:
    res = await client.post(
        "/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws_id},
    )
    assert res.status_code in (200, 201), res.text
    return res.json()["brand_profile_id"]


async def _generate(client, ws_id: str, brand_id: str, **overrides):
    body = {
        "title": "Launch quote", "brand_id": brand_id, "prompt": "a calm sunrise over hills",
        "headline": "Clarity beats scale", "accent_keyword": "Clarity",
    }
    body.update(overrides)
    return await client.post("/api/v1/image-assets/generate", json=body, headers={"X-Workspace-Id": ws_id})


async def _setup(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Image WS")
    return client, profile, ws_id, await _brand(client, ws_id)


def _h(ws_id: str) -> dict:
    return {"X-Workspace-Id": ws_id}


# ── layouts + generate ───────────────────────────────────────────────────────

async def test_layouts_lists_every_layout_with_real_dimensions(signup_user):
    client, _, ws_id, _ = await _setup(signup_user)

    res = await client.get("/api/v1/image-assets/layouts", headers=_h(ws_id))
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {layout.value for layout in LayoutPreset}
    assert len(body) == len(LayoutPreset)
    assert body["linkedin_post"] == {"width": 1200, "height": 627}
    assert body["x_post"] == {"width": 1600, "height": 900}
    assert body["social_share"] == {"width": 1200, "height": 630}
    assert body["youtube_thumbnail"] == {"width": 1280, "height": 720}
    assert body["instagram_square"] == {"width": 1080, "height": 1080}
    for layout, (w, h) in LAYOUT_DIMS.items():
        assert body[layout.value] == {"width": w, "height": h}


async def test_generate_persists_a_real_rendered_asset(signup_user, stubs):
    client, profile, ws_id, brand_id = await _setup(signup_user)

    res = await _generate(client, ws_id, brand_id)
    assert res.status_code == 201, res.text
    asset = res.json()

    assert asset["approval_status"] == "pending"
    assert asset["source_type"] == "ai_generated"
    assert asset["prompt"] == "a calm sunrise over hills"
    assert asset["created_by"] == profile["id"]
    assert len(asset["slides"]) == 1
    assert asset["slides"][0]["text_content"]["headline"] == "Clarity beats scale"

    media = await media_assets.find_one({"id": asset["slides"][0]["media_id"]})
    assert media["kind"] == "image"
    assert media["source"] == "rendered"
    assert media["workspace_id"] == ws_id

    # Two files are stored: the clean AI picture (no words on it, kept so edits never need a new one) and the
    # finished picture drawn from the layers. The finished one is the real render at the layout's size.
    assert len(stubs["uploads"]) == 2
    assert asset["slides"][0]["background_media_id"] and len(asset["slides"][0]["layers"]) >= 2
    rendered = Image.open(io.BytesIO(stubs["uploads"][-1]))
    assert rendered.size == LAYOUT_DIMS[LayoutPreset.QUOTE_1_1]
    assert (media["width"], media["height"]) == LAYOUT_DIMS[LayoutPreset.QUOTE_1_1]


@pytest.mark.parametrize("layout", list(LayoutPreset))
async def test_every_layout_renders_at_its_declared_size(signup_user, stubs, layout):
    client, _, ws_id, brand_id = await _setup(signup_user)

    res = await _generate(client, ws_id, brand_id, active_layout=layout.value)
    assert res.status_code == 201, res.text

    rendered = Image.open(io.BytesIO(stubs["uploads"][-1]))
    assert rendered.size == LAYOUT_DIMS[layout]


async def test_generate_validation_errors(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)

    no_prompt = await _generate(client, ws_id, brand_id, prompt=None)
    assert no_prompt.status_code == 400

    bad_brand = await _generate(client, ws_id, "no-such-brand")
    assert bad_brand.status_code == 404

    bad_source = await _generate(client, ws_id, brand_id, prompt=None, source_piece_id="nope")
    assert bad_source.status_code == 404
    assert stubs["uploads"] == [], "nothing may be rendered or uploaded on a rejected request"


async def test_generate_from_a_source_piece_derives_the_prompt(signup_user, stubs):
    client, profile, ws_id, brand_id = await _setup(signup_user)
    session_id = str(uuid4())
    await ensure_session_exists(
        session_id=session_id, workspace_id=ws_id, user_id=profile["id"],
        brand_id=brand_id, source_type="text",
    )
    piece_id = await save_live_piece(
        session_id=session_id, workspace_id=ws_id, user_id=profile["id"], brand_id=brand_id,
        platform="LinkedIn", content="Founders overestimate scale.\nSecond line.",
        word_count=6, char_count=44,
    )

    res = await _generate(client, ws_id, brand_id, prompt=None, source_piece_id=piece_id)
    assert res.status_code == 201, res.text
    assert res.json()["source_piece_id"] == piece_id
    assert res.json()["source_content_hash"]
    assert "Founders overestimate scale." in stubs["backgrounds"][-1]["prompt"]


async def test_viewers_cannot_generate(signup_user, make_client, stubs):
    owner, _, ws_id, brand_id = await _setup(signup_user)
    viewer, _ = await invite_and_accept(owner, make_client, ws_id, "viewer")

    res = await _generate(viewer, ws_id, brand_id)
    assert res.status_code == 403


# ── upload ───────────────────────────────────────────────────────────────────

async def test_upload_creates_an_uploaded_asset(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)

    res = await client.post(
        "/api/v1/image-assets/upload",
        data={"title": "My photo", "brand_id": brand_id},
        files={"file": ("photo.png", _png(), "image/png")},
        headers=_h(ws_id),
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["source_type"] == "uploaded"
    assert body["slides"][0]["slide_type"] == "upload"
    assert len(stubs["uploads"]) == 1


async def test_upload_gets_a_description_that_agents_can_read(signup_user, stubs, monkeypatch):
    """An uploaded image has no prompt, so nothing could say what it shows.
    Its description becomes alt text AND the text in the event Odette reads."""
    events: list = []
    monkeypatch.setattr(image_module, "emit_event_background", lambda **kw: events.append(kw))
    client, _, ws_id, brand_id = await _setup(signup_user)

    res = await client.post(
        "/api/v1/image-assets/upload",
        data={"title": "My photo", "brand_id": brand_id},
        files={"file": ("photo.png", _png(), "image/png")},
        headers=_h(ws_id),
    )
    assert res.status_code == 201, res.text
    assert res.json()["alt_text"] == "A blue square on a plain background."
    assert events[-1]["payload"].content_text == "A blue square on a plain background."


async def test_a_failed_description_never_blocks_the_upload(signup_user, stubs, monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("vision unavailable")

    monkeypatch.setattr(image_module, "call_vision", _boom)
    client, _, ws_id, brand_id = await _setup(signup_user)

    res = await client.post(
        "/api/v1/image-assets/upload",
        data={"title": "My photo", "brand_id": brand_id},
        files={"file": ("photo.png", _png(), "image/png")},
        headers=_h(ws_id),
    )
    assert res.status_code == 201, res.text
    assert res.json()["alt_text"] is None


async def test_an_exhausted_ai_budget_skips_the_description_but_not_the_upload(signup_user, stubs):
    from datetime import datetime, timezone

    from app.db.mongo import workspace_ai_budgets, workspace_ai_usage_daily

    client, _, ws_id, brand_id = await _setup(signup_user)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    await workspace_ai_budgets.insert_one({"id": ws_id, "workspace_id": ws_id, "monthly_token_budget": 10})
    await workspace_ai_usage_daily.insert_one(
        {"_id": f"{ws_id}:{today}", "id": f"{ws_id}:{today}", "workspace_id": ws_id, "date": today, "tokens_used": 10}
    )

    res = await client.post(
        "/api/v1/image-assets/upload",
        data={"title": "My photo", "brand_id": brand_id},
        files={"file": ("photo.png", _png(), "image/png")},
        headers=_h(ws_id),
    )
    assert res.status_code == 201, res.text
    assert res.json()["alt_text"] is None
    assert stubs["vision_calls"] == 0


async def test_upload_rejects_unsupported_file_types(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)

    res = await client.post(
        "/api/v1/image-assets/upload",
        data={"title": "Not an image", "brand_id": brand_id},
        files={"file": ("notes.txt", b"hello", "text/plain")},
        headers=_h(ws_id),
    )
    assert res.status_code == 400
    assert stubs["uploads"] == []


# ── carousel CRUD ────────────────────────────────────────────────────────────

async def _asset_with_slides(client, ws_id: str, brand_id: str, extra: int) -> dict:
    asset = (await _generate(client, ws_id, brand_id)).json()
    for i in range(extra):
        res = await client.post(
            f"/api/v1/image-assets/{asset['id']}/slides",
            json={"headline": f"Slide {i + 2}"}, headers=_h(ws_id),
        )
        assert res.status_code == 201, res.text
        asset = res.json()
    return asset


async def test_add_slide_appends_renders_and_bumps_the_version(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _asset_with_slides(client, ws_id, brand_id, extra=1)

    assert [s["slide_number"] for s in asset["slides"]] == [1, 2]
    assert asset["slides"][1]["text_content"]["headline"] == "Slide 2"
    assert asset["version_count"] == 2
    # Reuses the parent's own prompt when the caller gives none.
    assert stubs["backgrounds"][-1]["prompt"] == "a calm sunrise over hills"


async def test_remove_slide_renumbers_and_protects_the_last_one(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _asset_with_slides(client, ws_id, brand_id, extra=2)
    url = f"/api/v1/image-assets/{asset['id']}/slides"

    res = await client.delete(f"{url}/2", headers=_h(ws_id))
    assert res.status_code == 200, res.text
    slides = res.json()["slides"]
    assert [s["slide_number"] for s in slides] == [1, 2]
    assert slides[1]["text_content"]["headline"] == "Slide 3"  # old slide 3, renumbered

    assert (await client.delete(f"{url}/9", headers=_h(ws_id))).status_code == 404

    await client.delete(f"{url}/2", headers=_h(ws_id))
    last = await client.delete(f"{url}/1", headers=_h(ws_id))
    assert last.status_code == 400


async def test_reorder_slides(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _asset_with_slides(client, ws_id, brand_id, extra=2)
    url = f"/api/v1/image-assets/{asset['id']}/slides/reorder"

    res = await client.patch(url, json={"order": [3, 1, 2]}, headers=_h(ws_id))
    assert res.status_code == 200, res.text
    headlines = [s["text_content"]["headline"] for s in res.json()["slides"]]
    assert headlines == ["Slide 3", "Clarity beats scale", "Slide 2"]
    assert [s["slide_number"] for s in res.json()["slides"]] == [1, 2, 3]

    for bad in ([1, 2], [1, 2, 3, 4], [1, 1, 2]):
        res = await client.patch(url, json={"order": bad}, headers=_h(ws_id))
        assert res.status_code == 400, bad


# ── governance ───────────────────────────────────────────────────────────────

async def test_approve_pins_the_master_render_and_reject_overrides(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    base = f"/api/v1/image-assets/{asset['id']}"

    res = await client.patch(f"{base}/approve", headers=_h(ws_id))
    assert res.status_code == 200, res.text
    assert res.json()["approval_status"] == "approved"
    assert res.json()["approved_master_media_id"] == asset["slides"][0]["media_id"]

    res = await client.patch(f"{base}/reject", headers=_h(ws_id))
    assert res.json()["approval_status"] == "rejected"


async def test_editors_can_edit_but_not_approve(signup_user, make_client, stubs):
    owner, _, ws_id, brand_id = await _setup(signup_user)
    editor, _ = await invite_and_accept(owner, make_client, ws_id, "editor")
    asset = (await _generate(owner, ws_id, brand_id)).json()

    res = await editor.patch(f"/api/v1/image-assets/{asset['id']}/approve", headers=_h(ws_id))
    assert res.status_code == 403

    added = await editor.post(
        f"/api/v1/image-assets/{asset['id']}/slides", json={"headline": "By editor"}, headers=_h(ws_id),
    )
    assert added.status_code == 201, added.text


async def test_version_history_and_restore_round_trip(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = await _asset_with_slides(client, ws_id, brand_id, extra=1)  # now v2, two slides
    base = f"/api/v1/image-assets/{asset['id']}"

    versions = (await client.get(f"{base}/versions", headers=_h(ws_id))).json()
    assert versions["total"] >= 1
    assert [v["version_number"] for v in versions["versions"]] == sorted(
        v["version_number"] for v in versions["versions"]
    )

    # Restoring the ORIGINAL single-slide state must work: version 1 is what
    # every user goes back to after experimenting with a carousel.
    res = await client.post(f"{base}/restore/1", headers=_h(ws_id))
    assert res.status_code == 200, res.text
    assert len(res.json()["slides"]) == 1
    assert res.json()["slides"][0]["text_content"]["headline"] == "Clarity beats scale"
    assert res.json()["version_count"] == 3  # a restore is itself a new version, never a rewind


async def test_restore_of_an_unknown_version_is_404(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()

    res = await client.post(f"/api/v1/image-assets/{asset['id']}/restore/99", headers=_h(ws_id))
    assert res.status_code == 404


async def test_share_link_has_a_token_url_and_30_day_expiry(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()

    res = await client.post(f"/api/v1/image-assets/{asset['id']}/share-link", headers=_h(ws_id))
    assert res.status_code == 201, res.text
    body = res.json()
    assert len(body["token"]) >= 24
    assert body["url"].endswith(f"/share/{body['token']}")
    assert body["expires_at"]


# ── export ───────────────────────────────────────────────────────────────────

async def test_export_creates_a_distinct_derivative_and_rejects_unknown_formats(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    url = f"/api/v1/image-assets/{asset['id']}/export"

    res = await client.post(url, json={"export_format": "webp"}, headers=_h(ws_id))
    assert res.status_code == 201, res.text
    assert res.json()["mime_type"] == "image/webp"
    assert res.json()["source"] == "edited"
    assert res.json()["id"] != asset["slides"][0]["media_id"]  # a new asset, original untouched

    bad = await client.post(url, json={"export_format": "svg"}, headers=_h(ws_id))
    assert bad.status_code == 400


# ── scoping ──────────────────────────────────────────────────────────────────

async def test_assets_are_invisible_across_workspaces(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    other_ws = await create_workspace(client, "Other Image WS")

    for method, path in (
        ("patch", f"/api/v1/image-assets/{asset['id']}/approve"),
        ("get", f"/api/v1/image-assets/{asset['id']}/versions"),
        ("post", f"/api/v1/image-assets/{asset['id']}/share-link"),
    ):
        res = await getattr(client, method)(path, headers=_h(other_ws))
        assert res.status_code == 404, (method, path)


# ── Library list ─────────────────────────────────────────────────────────────

async def test_list_returns_the_workspaces_images_with_a_preview(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    created = (await _generate(client, ws_id, brand_id)).json()

    res = await client.get("/api/v1/image-assets/", headers=_h(ws_id))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == created["id"]
    assert item["slide_count"] == 1
    assert item["media"]["kind"] == "image"
    assert "_id" not in item["media"]


async def test_list_never_shows_another_workspaces_images(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    await _generate(client, ws_id, brand_id)

    other = await create_workspace(client, "Other WS")
    res = await client.get("/api/v1/image-assets/", headers=_h(other))
    assert res.status_code == 200
    assert res.json() == {"items": [], "total": 0}


# ── Icons ────────────────────────────────────────────────────────────────────

async def test_icons_endpoint_lists_the_available_icons(signup_user):
    client, _, ws_id, _ = await _setup(signup_user)

    res = await client.get("/api/v1/image-assets/icons", headers=_h(ws_id))
    assert res.status_code == 200
    icons_list = res.json()["icons"]
    assert len(icons_list) > 1000
    assert {"name", "cp", "tags"} <= set(icons_list[0])


async def test_generate_with_an_icon_stores_it_and_renders_it(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)

    plain = await _generate(client, ws_id, brand_id)
    with_icon = await _generate(client, ws_id, brand_id, icon_name="rocket", illustration_accent=True)
    assert plain.status_code == 201 and with_icon.status_code == 201

    text = with_icon.json()["slides"][0]["text_content"]
    assert text["icon_name"] == "rocket"
    assert text["illustration_accent"] is True
    # each generation stores the clean picture then the finished one: compare the two finished pictures
    assert stubs["uploads"][1] != stubs["uploads"][3], "the icon must change the rendered image"


async def test_generate_rejects_an_unknown_icon_before_rendering(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)

    res = await _generate(client, ws_id, brand_id, icon_name="definitely-not-an-icon")
    assert res.status_code == 400
    assert "icon" in res.json()["detail"].lower()
    assert stubs["uploads"] == [], "nothing may be rendered or uploaded on a rejected request"
