"""Two open onboarding tabs must not silently overwrite each other (PAR-009). A tab that does not send its
name keeps the old behaviour."""

from tests.conftest import signup_new_user

URL = "/api/v1/onboarding/draft"


def body(step, tab=None, base=None):
    data = {"brand_type": "Business", "current_step": step, "total_steps": 7}
    if tab:
        data["client_id"] = tab
    if base:
        data["base_updated_at"] = base
    return data


async def test_a_tab_can_keep_saving_over_its_own_saves(api_client):
    await signup_new_user(api_client)
    first = (await api_client.post(URL, json=body(1, "tab-a"))).json()
    # the same tab saves again, even with an out-of-date base (two saves in flight)
    second = await api_client.post(URL, json=body(2, "tab-a", first["updated_at"]))
    third = await api_client.post(URL, json=body(3, "tab-a", first["updated_at"]))
    assert second.status_code == 200 and third.status_code == 200
    assert third.json()["current_step"] == 3


async def test_another_tab_that_missed_a_newer_save_is_refused(api_client):
    await signup_new_user(api_client)
    seen_by_b = (await api_client.post(URL, json=body(1, "tab-b"))).json()
    await api_client.post(URL, json=body(4, "tab-a", seen_by_b["updated_at"]))  # tab A moves on
    late = await api_client.post(URL, json=body(2, "tab-b", seen_by_b["updated_at"]))
    assert late.status_code == 409
    assert late.json()["detail"]["code"] == "draft_changed_elsewhere"
    # nothing was overwritten
    assert (await api_client.get(URL)).json()["current_step"] == 4


async def test_another_tab_that_has_the_latest_save_goes_through(api_client):
    await signup_new_user(api_client)
    saved_by_a = (await api_client.post(URL, json=body(3, "tab-a"))).json()
    ok = await api_client.post(URL, json=body(4, "tab-b", saved_by_a["updated_at"]))
    assert ok.status_code == 200


async def test_saves_without_a_tab_name_behave_as_before(api_client):
    await signup_new_user(api_client)
    await api_client.post(URL, json=body(1, "tab-a"))
    plain = await api_client.post(URL, json=body(2))
    assert plain.status_code == 200
    assert plain.json()["current_step"] == 2
