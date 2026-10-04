"""Ops banners a staff member has closed: stored per person, never shared, and validated."""

import pytest

from app.db.mongo import users
from tests.conftest import signup_new_user

B = "/api/v1/ops/banners"


async def _staff(make_client):
    client = make_client()
    user = await signup_new_user(client, name="Banner Person")
    await users.update_one({"id": user["id"]}, {"$set": {"is_platform_staff": True}})
    return client, user


async def test_nothing_is_closed_at_first(make_client):
    client, _ = await _staff(make_client)
    res = await client.get(B)
    assert res.status_code == 200 and res.json() == {"banners": {}}


async def test_closing_a_banner_keeps_the_items_it_was_about(make_client):
    client, _ = await _staff(make_client)
    saved = await client.put(f"{B}/catalog-attention", json={"dismissed": True, "items": ["youtube", " google ", "youtube", ""]})
    assert saved.status_code == 200 and saved.json()["items"] == ["youtube", "google"]
    got = (await client.get(B)).json()["banners"]
    assert got["catalog-attention"] == {"dismissed": True, "items": ["youtube", "google"]}


async def test_bringing_a_banner_back_clears_it(make_client):
    client, _ = await _staff(make_client)
    await client.put(f"{B}/paused:slack", json={"dismissed": True, "items": ["paused"]})
    back = await client.put(f"{B}/paused:slack", json={"dismissed": False})
    assert back.status_code == 200
    assert (await client.get(B)).json()["banners"]["paused:slack"] == {"dismissed": False, "items": []}


async def test_each_person_has_their_own_closed_banners(make_client):
    first, _ = await _staff(make_client)
    second, _ = await _staff(make_client)
    await first.put(f"{B}/catalog-attention", json={"dismissed": True, "items": ["youtube"]})
    assert (await second.get(B)).json() == {"banners": {}}


@pytest.mark.parametrize("bad", ["Bad Name", "a.b", "x" * 81, "UPPER"])
async def test_a_banner_name_must_be_plain(make_client, bad):
    client, _ = await _staff(make_client)
    res = await client.put(f"{B}/{bad}", json={"dismissed": True, "items": []})
    assert res.status_code in (404, 422)


async def test_too_many_items_are_refused(make_client):
    client, _ = await _staff(make_client)
    res = await client.put(f"{B}/catalog-attention", json={"dismissed": True, "items": [str(i) for i in range(201)]})
    assert res.status_code == 422


async def test_only_staff_can_use_banners(make_client):
    client = make_client()
    await signup_new_user(client)
    assert (await client.get(B)).status_code == 403
    assert (await client.put(f"{B}/catalog-attention", json={"dismissed": True, "items": []})).status_code == 403
