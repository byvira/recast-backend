"""The saved set of example drafts: what can be saved, that running it shows which examples behave differently after a change, and that it
costs nothing (only the free checks run)."""
import pytest

from app.core import staff as ST
from app.db.mongo import get_db, users
from app.quality import reference as R
from app.quality import settings as QS

GOOD = " ".join(["plain"] * 200) + ". What do you think?"
SHORT = "Too short to count as a real LinkedIn post, honestly."


@pytest.fixture(autouse=True)
async def clean():
    for col in (R.col(), get_db()["quality_settings"], get_db()["quality_history"]):
        await col.delete_many({})
    QS.reset_cache()
    yield
    for col in (R.col(), get_db()["quality_settings"], get_db()["quality_history"]):
        await col.delete_many({})
    QS.reset_cache()


async def _add(platform="LinkedIn", text=GOOD, expect="pass", language="en"):
    return await R.add(platform=platform, language=language, text=text, expect=expect, note="", user_id="u1")


# ---- what can be saved ------------------------------------------------------------------------------------------------------------
async def test_a_draft_is_saved_with_what_should_happen_to_it():
    row = await R.add(platform="LinkedIn", language=" EN ", text=GOOD, expect="pass", note="  a   good one ", user_id="u1")
    assert row["language"] == "en" and row["note"] == "a good one" and row["expect"] == "pass"
    assert len(await R.listing()) == 1


@pytest.mark.parametrize("kwargs, words", [
    ({"platform": "Myspace"}, "not a platform"),
    ({"expect": "maybe"}, "pass or be held"),
    ({"text": "short"}, "Paste the draft"),
    ({"text": "x" * 7000}, "up to 6000"),
])
async def test_bad_examples_are_refused_with_plain_words(kwargs, words):
    with pytest.raises(R.ReferenceError) as err:
        await _add(**kwargs)
    assert words in str(err.value)


async def test_the_set_has_limits(monkeypatch):
    monkeypatch.setattr(R, "PER_PLATFORM_MAX", 2)
    await _add()
    await _add()
    with pytest.raises(R.ReferenceError) as err:
        await _add()
    assert "LinkedIn can hold up to 2" in str(err.value)
    await _add(platform="Blog", text=GOOD)
    monkeypatch.setattr(R, "TOTAL_MAX", 3)
    with pytest.raises(R.ReferenceError):
        await _add(platform="Instagram")


async def test_a_draft_can_be_removed():
    row = await _add()
    assert await R.remove(row["id"]) is True and await R.remove(row["id"]) is False


# ---- running it --------------------------------------------------------------------------------------------------------------------
async def test_running_shows_which_drafts_behave_as_expected():
    await _add(text=GOOD, expect="pass")
    await _add(text=SHORT, expect="held")
    await _add(text=SHORT, expect="pass")  # wrong on purpose: it is held
    out = await R.run()
    assert out["total"] == 3 and out["as_expected"] == 2 and out["different"] == 1 and out["using"] == "the saved values"
    assert out["results"][0]["as_expected"] is False and out["results"][0]["outcome"] == "held" and "too short" in out["results"][0]["reasons"][0].lower()


async def test_trying_a_change_shows_the_effect_without_saving_it():
    await _add(text=SHORT, expect="held")
    saved = await R.run()
    assert saved["different"] == 0
    tried = await R.run({"min_words": {"LinkedIn": 5}})
    assert tried["using"] == "your unsaved values" and tried["different"] == 1 and tried["results"][0]["outcome"] == "pass"
    assert QS.min_words("LinkedIn") == 150, "nothing was saved"
    assert (await R.run())["different"] == 0


async def test_a_bad_change_is_refused_when_running():
    await _add()
    with pytest.raises(QS.SettingsError):
        await R.run({"min_words": {"LinkedIn": 99999}})


async def test_the_safety_rules_are_part_of_the_run():
    await _add(text=GOOD + " I will kill you", expect="held")
    assert (await R.run())["different"] == 0


async def test_running_calls_no_model(monkeypatch):
    from unittest.mock import AsyncMock

    boom = AsyncMock(side_effect=AssertionError("a model must not be called"))
    monkeypatch.setattr("app.shared.llm.call_llm", boom)
    monkeypatch.setattr("app.shared.llm.call_llm_structured", boom)
    await _add()
    await R.run()
    assert not boom.called


# ---- who can use it -------------------------------------------------------------------------------------------------------------------
async def test_the_routes_follow_the_quality_area(signup_user):
    await ST.ensure_templates()
    analyst, profile = await signup_user("Analyst")
    await users.update_one({"id": profile["id"]}, {"$set": {"is_platform_staff": True, "staff": {"level": "staff", "role_id": "quality_analyst", "status": "active"}}})
    ST.forget()
    added = await analyst.post("/api/v1/ops/quality/reference", json={"platform": "LinkedIn", "language": "en", "text": GOOD, "expect": "pass"})
    assert added.status_code == 200, added.text
    listed = (await analyst.get("/api/v1/ops/quality/reference")).json()
    assert len(listed["items"]) == 1 and listed["limits"]["total"] == R.TOTAL_MAX
    ran = (await analyst.post("/api/v1/ops/quality/reference/run", json={})).json()
    assert ran["total"] == 1 and ran["different"] == 0
    bad = await analyst.post("/api/v1/ops/quality/reference/run", json={"changes": {"min_words": {"LinkedIn": -3}}})
    assert bad.status_code == 422
    assert (await analyst.post("/api/v1/ops/quality/reference", json={"platform": "Myspace", "text": GOOD, "expect": "pass"})).status_code == 422
    gone = await analyst.delete(f"/api/v1/ops/quality/reference/{added.json()['id']}")
    assert gone.status_code == 200 and (await analyst.delete(f"/api/v1/ops/quality/reference/{added.json()['id']}")).status_code == 404

    customer, _ = await signup_user("Customer")
    assert (await customer.get("/api/v1/ops/quality/reference")).status_code == 403
