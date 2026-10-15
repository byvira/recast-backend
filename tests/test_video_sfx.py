"""The sound effects pack: every effect is there with its CC0 licence record and a matching checksum, served only by catalog name, and added to a
workspace once."""
import hashlib
from uuid import uuid4

from app.db.mongo import media_assets
from app.video import sfx
from tests.conftest import create_workspace

S = "/api/v1/video-sfx"


def test_the_pack_has_twelve_effects_each_with_a_file_a_matching_checksum_and_a_cc0_record():
    effects = sfx.catalog()
    assert len(effects) == 12 and len({e["id"] for e in effects}) == 12
    for e in effects:
        data = sfx.path_of(e["id"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == e["sha256"] and len(data) == e["bytes"] > 1000
        assert 0.03 < e["duration_s"] < 2.5 and e["category"] in {"whoosh", "hit", "build", "tone"}
        assert e["licence"]["url"] == "https://creativecommons.org/publicdomain/zero/1.0/" and e["licence"]["author"] == "Ekavo" and "Synthesised" in e["licence"]["origin"]


def test_only_a_catalog_name_gives_a_file_never_a_path():
    assert sfx.path_of("pop") is not None and sfx.get("pop")["title"] == "Pop"
    for bad in ("", "../catalog", "..\\x", "pop.mp3", "POP", "nope", "a" * 60, "pop/../hit"):
        assert sfx.get(bad) is None and sfx.path_of(bad) is None


async def test_the_list_names_each_effect_with_its_licence_and_the_file_is_public_and_cacheable(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Sfx {uuid4().hex[:6]}")

    listed = (await client.get(S, headers={"X-Workspace-Id": ws})).json()["effects"]
    assert len(listed) == 12 and listed[0]["licence"].startswith("CC0") and listed[0]["preview_url"].endswith(f"/api/v1/video-sfx/files/{listed[0]['id']}.mp3")

    got = await client.get(f"{S}/files/pop.mp3")                                  # no sign-in: the render worker fetches it this way
    assert got.status_code == 200 and got.headers["content-type"] == "audio/mpeg" and "immutable" in got.headers["cache-control"] and len(got.content) > 1000
    assert (await client.get(f"{S}/files/nope.mp3")).status_code == 404
    assert (await client.get(f"{S}/files/pop")).status_code == 404
    assert (await client.get(f"{S}/files/..%2Fcatalog.json")).status_code == 404


async def test_adding_an_effect_puts_one_item_in_the_library_with_its_licence_and_adding_again_returns_it(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Sfx {uuid4().hex[:6]}")
    other = await create_workspace(client, f"Other {uuid4().hex[:6]}")
    h = {"X-Workspace-Id": ws}

    first = await client.post(f"{S}/pop/add", headers=h)
    second = await client.post(f"{S}/pop/add", headers=h)
    elsewhere = await client.post(f"{S}/pop/add", headers={"X-Workspace-Id": other})

    assert first.status_code == 201 and first.json()["created"] is True and first.json()["duration_s"] == 0.16
    assert second.json() == {"media_id": first.json()["media_id"], "duration_s": 0.16, "created": False}
    assert elsewhere.json()["media_id"] != first.json()["media_id"]
    row = await media_assets.find_one({"id": first.json()["media_id"]})
    assert row["kind"] == "audio" and row["origin"] == "own" and row["contains_stock"] is False and row["url"].endswith("/files/pop.mp3")
    assert row["stock"]["licence"].startswith("CC0") and row["stock"]["author"] == "Ekavo"
    assert (await client.post(f"{S}/nope/add", headers=h)).status_code == 404
