"""A mascot that was not drawn must be reported as not made, so its run shows as not finished instead of done."""

from app.api.v1 import brand as brand_module
from app.db.mongo import brand_profiles


class _Asset:
    url = "https://example.com/mascot.png"


async def _seed(brand_id: str) -> None:
    await brand_profiles.delete_many({"id": brand_id})
    await brand_profiles.insert_one({"id": brand_id, "workspace_id": "w-mascot", "mascot_pending": True, "visual_identity": {}})


async def test_no_picture_back_means_not_made_and_the_pending_mark_is_cleared(monkeypatch):
    await _seed("mascot-none")

    async def _nothing(**kwargs):
        return None

    monkeypatch.setattr(brand_module, "generate_brand_mascot", _nothing)
    assert await brand_module._generate_and_save_mascot("mascot-none", "w-mascot", "u-mascot") is False
    doc = await brand_profiles.find_one({"id": "mascot-none"})
    assert "mascot_pending" not in doc
    assert not (doc.get("visual_identity") or {}).get("mascot_url")
    await brand_profiles.delete_many({"id": "mascot-none"})


async def test_a_picture_back_is_saved_and_reported_as_made(monkeypatch):
    await _seed("mascot-ok")

    async def _made(**kwargs):
        return _Asset()

    monkeypatch.setattr(brand_module, "generate_brand_mascot", _made)
    assert await brand_module._generate_and_save_mascot("mascot-ok", "w-mascot", "u-mascot") is True
    doc = await brand_profiles.find_one({"id": "mascot-ok"})
    assert doc["visual_identity"]["mascot_url"] == "https://example.com/mascot.png"
    await brand_profiles.delete_many({"id": "mascot-ok"})


def test_the_message_a_failed_mascot_run_shows_is_plain_words():
    assert "couldn't draw a mascot" in brand_module.MASCOT_NOT_MADE.lower()
