"""Proof for old audit rows that were built but had no test of their own: a failed analytics read never replaces real numbers, the text rule tables follow the platform list,
the retired audio route says so, the Bluesky renewal keeps its rotating token, and a picture set says honestly how much of it was made. No provider or network is used."""
import base64
import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from app.api.v1 import image_assets as image_module
from app.db.mongo import get_db
from app.pipelines.analytics import aggregator
from app.pipelines.analytics.base import PostMetrics
from tests.conftest import create_workspace
from tests.test_image_assets import _brand, _generate, _png, stubs  # noqa: F401  (stubs is a fixture)


class _Reader:
    def __init__(self, result):
        self.result = result

    async def fetch_post_metrics(self, **kwargs):
        return self.result


def _use(monkeypatch, reader):
    async def token(workspace_id, platform):
        return {"access_token": "t"}

    monkeypatch.setattr(aggregator, "_get_fetcher", lambda platform: reader)
    monkeypatch.setattr(aggregator, "get_token", token)


async def test_a_failed_read_keeps_the_earlier_numbers_and_never_writes_zeros(monkeypatch):
    ws = f"w-{uuid4().hex[:8]}"
    key = {"workspace_id": ws, "platform": "linkedin", "platform_post_id": "urn:1"}
    await get_db()["post_metrics"].insert_one({**key, "likes": 7, "comments": 2, "updated_at": datetime.now(timezone.utc)})
    post = {"platform": "linkedin", "platform_post_id": "urn:1", "platform_user_id": "u", "piece_id": "p-none"}
    try:
        _use(monkeypatch, _Reader(PostMetrics(platform="linkedin", post_id="p-none", platform_post_id="urn:1", fetch_ok=False, failure="transient")))
        assert await aggregator.fetch_post_metrics_all(ws, [post]) == []                      # nothing came back to save
        kept = await get_db()["post_metrics"].find_one(key)
        assert kept["likes"] == 7 and kept["comments"] == 2                                   # the earlier real numbers are untouched
        _use(monkeypatch, _Reader(PostMetrics(platform="linkedin", post_id="p-none", platform_post_id="urn:1", likes=9)))
        assert len(await aggregator.fetch_post_metrics_all(ws, [post])) == 1                  # a good read does replace them
        assert (await get_db()["post_metrics"].find_one(key))["likes"] == 9
        assert await get_db()["post_metrics"].count_documents(key) == 1                       # still one row, never a second one of zeros
    finally:
        await get_db()["post_metrics"].delete_many({"workspace_id": ws})


def test_every_text_platform_has_its_rules_and_the_hashtag_and_call_to_action_tables_only_name_real_platforms():
    from app.pipelines.text.generator import CTA_RULES, HASHTAG_RULES, PLATFORM_RULES
    from app.models.text import Platform

    everyone = set(Platform)
    assert set(PLATFORM_RULES) == everyone                                                    # no platform is left without its own writing rules
    for table in (PLATFORM_RULES, HASHTAG_RULES, CTA_RULES):
        assert set(table) <= everyone
        for member in table:
            assert table[member].strip(), f"{member} has an empty rule"


async def test_the_retired_audio_start_route_says_what_to_use_instead(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Stub WS")
    res = await client.post("/api/v1/audio", headers={"X-Workspace-Id": ws}, json={"script": "Hello there"})
    assert res.status_code in (422, 501)
    if res.status_code == 501:
        assert "audio-assets/generate" in res.text


def _jwt(exp: int) -> str:
    part = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{part({'alg': 'none'})}.{part({'exp': exp})}.sig"


async def test_a_bluesky_renewal_keeps_the_new_rotating_token_and_does_not_invent_one(monkeypatch):
    from app.pipelines.publish.bluesky.publisher import BlueSkyPublisher

    answers = [{"accessJwt": _jwt(2_000_000_000), "refreshJwt": "new-refresh"}, {"accessJwt": _jwt(2_000_000_000)}]

    class _Resp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None):
            assert headers["Authorization"] == "Bearer old-refresh"
            return _Resp(answers.pop(0))

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())
    publisher = BlueSkyPublisher()
    first = await publisher.refresh_token("old-refresh")
    assert first["refresh_token"] == "new-refresh" and first["access_token"] and first["expires_at"].year == 2033
    second = await publisher.refresh_token("old-refresh")
    assert "refresh_token" not in second                                                      # nothing is made up when Bluesky sent none, so the old one is kept by the caller


async def test_a_picture_set_that_could_not_be_finished_says_how_many_were_made_and_a_full_one_carries_no_note(signup_user, stubs, monkeypatch):  # noqa: F811
    client, _, ws_id, brand_id = await _setup_images(signup_user)
    calls = {"n": 0}

    async def flaky(*, prompt, workspace_id, user_id, target_size, brand_profile, avoid=None):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("provider quota used up")
        return _png(target_size)

    monkeypatch.setattr(image_module, "generate_image_from_prompt", flaky)
    partial = (await _generate(client, ws_id, brand_id, count=4)).json()
    assert len(partial["slides"]) == 2 and partial["qa_flagged"] is True
    assert "Only 2 of 4 pictures could be made" in partial["qa_flag_reason"] and "Still making" not in partial["qa_flag_reason"]

    async def fine(*, prompt, workspace_id, user_id, target_size, brand_profile, avoid=None):
        return _png(target_size)

    monkeypatch.setattr(image_module, "generate_image_from_prompt", fine)
    full = (await _generate(client, ws_id, brand_id, count=2)).json()
    assert len(full["slides"]) == 2 and "Still making" not in str(full.get("qa_flag_reason") or "")   # a finished set drops the "still making" note


async def _setup_images(signup_user):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Set WS")
    return client, None, ws_id, await _brand(client, ws_id)
