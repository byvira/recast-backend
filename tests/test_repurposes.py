from app.db.mongo import audio_assets
from tests.conftest import create_workspace, signup_new_user


def _h(ws_id: str) -> dict:
    return {"X-Workspace-Id": ws_id}


async def test_results_are_recorded_listed_a_page_at_a_time_and_unlinked(api_client):
    await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Repurpose Record", tier="large")
    await audio_assets.insert_one({"id": "out-1", "workspace_id": ws, "title": "Narrated version", "approval_status": "approved"})

    outputs = [{"kind": "audio", "id": "out-1"}] + [{"kind": "text", "id": f"gone-{i}", "label": f"Post {i}"} for i in range(5)]
    first = await api_client.post("/api/v1/repurposes", json={"source_kind": "audio", "source_id": "src", "outputs": outputs}, headers=_h(ws))
    assert first.status_code == 201, first.text
    assert first.json()["recorded"] == 6
    # recording the same results again keeps one row each
    again = await api_client.post("/api/v1/repurposes", json={"source_kind": "audio", "source_id": "src", "outputs": outputs}, headers=_h(ws))
    assert again.json()["recorded"] == 0

    params = {"source_kind": "audio", "source_id": "src"}
    page1 = (await api_client.get("/api/v1/repurposes", params=params, headers=_h(ws))).json()
    assert page1["total"] == 6 and len(page1["items"]) == 4
    page2 = (await api_client.get("/api/v1/repurposes", params={**params, "offset": 4}, headers=_h(ws))).json()
    assert len(page2["items"]) == 2

    everything = {i["output_id"]: i for i in page1["items"] + page2["items"]}
    assert everything["out-1"]["title"] == "Narrated version" and everything["out-1"]["status"] == "approved"
    assert everything["out-1"]["href"] == "/dashboard/pipelines/audio?asset=out-1"
    assert everything["gone-0"]["status"] == "removed"  # no such post any more: said so, not hidden

    other = (await api_client.get("/api/v1/repurposes", params={**params, "source_id": "someone-else"}, headers=_h(ws))).json()
    assert other["total"] == 0

    gone = await api_client.delete(f"/api/v1/repurposes/{everything['out-1']['id']}", headers=_h(ws))
    assert gone.status_code == 200
    assert (await audio_assets.find_one({"id": "out-1"})) is not None  # the result itself is untouched
    left = (await api_client.get("/api/v1/repurposes", params=params, headers=_h(ws))).json()
    assert left["total"] == 5
