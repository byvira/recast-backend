from app.shared.activity import runs
from tests.conftest import create_workspace, signup_new_user


async def test_a_run_that_stopped_reporting_leaves_the_board(api_client, monkeypatch):
    await signup_new_user(api_client)
    ws = await create_workspace(api_client, "Silent run", tier="large")
    await runs.start_run(workspace_id=ws, run_id="r1", kind="audio", title="Cleaning up audio", steps_total=3)
    assert [r["id"] for r in await runs.list_runs(ws)] == ["r1"]

    real = runs.time.time
    monkeypatch.setattr(runs.time, "time", lambda: real() + runs.SILENT_AFTER_SECONDS + 60)
    assert await runs.list_runs(ws) == []
    monkeypatch.undo()

    # one that keeps reporting stays
    await runs.start_run(workspace_id=ws, run_id="r2", kind="audio", title="Narration", steps_total=3)
    await runs.update_run(ws, "r2", stage="Voicing", steps_done=1)
    assert [r["id"] for r in await runs.list_runs(ws)] == ["r2"]
    await runs.end_run(ws, "r2")
