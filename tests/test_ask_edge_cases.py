"""Edge cases of a running Ask request that the plain flow does not touch: the price moves between the plan and the yes, Stop is pressed while it runs, and the server restarts
while it runs. Every model answer is a stub; no tool does real work."""
from datetime import datetime, timezone
from uuid import uuid4

from app.ask import runs
from app.billing import costs as credit_costs
from app.tools import compose
from app.tools.contract import Risk
from app.tools.registry import get_registry
from tests.test_ask import _free_read, _stub, asker  # noqa: F401  (asker is a fixture)


async def _plan_with_a_price(client, h, monkeypatch):
    table = await credit_costs.costs()
    priced = next(s for s in get_registry().all() if s.cost_key and credit_costs.price_with(table, s.cost_key, 1.0) > 0 and s.risk is not Risk.PUBLISH
                  and not s.human_only and s.scope.value in ("create",) and not s.name.startswith(("compose.", "changes.")) and not (s.input_schema() or {}).get("required"))
    _stub(monkeypatch, {"decide": {"action": "tool", "tool": priced.name}})

    async def no_problems(gate, cred, steps):
        return []

    monkeypatch.setattr(runs, "_check_steps", no_problems)
    body = (await client.post("/api/v1/ask", headers=h, json={"message": priced.summary})).json()
    assert body["status"] == "ready" and body["plan"]["credits"] > 0
    return body


async def _running_run(ws: str, user: str, tool: str, **over) -> str:
    """A run that is under way, written straight to the store so the test controls exactly when it stops."""
    run_id = uuid4().hex
    now = datetime.now(timezone.utc)
    await runs.runs().insert_one({
        "id": run_id, "workspace_id": ws, "user_id": user, "message": "do the thing", "trust": "ask", "status": "running", "language": "en", "created_at": now, "updated_at": now,
        "started_at": now, "plan": {"steps": [{"label": "one", "tool": tool, "arguments": {}}], "credits": 0, "exact": True}, **over,
    })
    return run_id


async def test_a_price_that_moves_between_the_plan_and_the_yes_runs_nothing_and_the_new_price_must_be_confirmed(asker, monkeypatch):
    client, user, ws, h = asker
    body = await _plan_with_a_price(client, h, monkeypatch)
    seen_price = body["plan"]["credits"]

    async def dearer(steps):
        return [], seen_price + 7, True

    monkeypatch.setattr(runs, "_price", dearer)
    ran: list[str] = []

    async def fake_steps(workspace_id, user_id, run_id, steps, services):
        ran.append(run_id)
        await runs.runs().update_one({"id": run_id}, {"$set": {"status": "done", "result": {"status": "done", "steps": []}}})

    monkeypatch.setattr(runs, "_execute_steps", fake_steps)
    first = await client.post(f"/api/v1/ask/{body['id']}/confirm", headers=h)
    assert first.status_code == 409 and f"The price changed to {seen_price + 7} credits" in first.text
    row = await runs.runs().find_one({"id": body["id"]})
    assert row["status"] == "ready" and row["plan"]["credits"] == seen_price + 7 and ran == []          # nothing ran, the plan now shows the new price
    second = await client.post(f"/api/v1/ask/{body['id']}/confirm", headers=h)                           # the person has now seen the new price
    assert second.status_code == 200, second.text
    for _ in range(20):
        if ran:
            break
        import asyncio

        await asyncio.sleep(0.1)
    assert ran == [body["id"]]


async def test_stop_pressed_while_a_request_runs_lets_the_step_under_way_finish_and_no_later_step_start(asker):
    client, user, ws, h = asker
    tool = _free_read()
    run_id = await _running_run(ws, user, tool.name)
    stopped = await client.post(f"/api/v1/ask/{run_id}/stop", headers=h)
    assert stopped.status_code == 200 and stopped.json()["status"] == "running"                          # a running request is asked to stop, not cut off
    assert (await runs.runs().find_one({"id": run_id}))["stop_requested"] is True
    steps = [compose.Step(label="one", tool=tool.name, arguments={}), compose.Step(label="two", tool=tool.name, arguments={})]
    await runs._execute_steps(ws, user, run_id, steps, services=None)
    row = await runs.runs().find_one({"id": run_id})
    assert row["status"] == "stopped"
    steps_out = row["result"]["steps"]
    assert (steps_out[0].get("error") or {}).get("code") == "stopped"                                    # the first step was refused at the door
    assert not any(s["state"] == "done" for s in steps_out)                                              # and neither step did any work
    shown = (await client.get(f"/api/v1/ask/{run_id}", headers=h)).json()
    assert shown["status"] == "stopped" and "stopped" in str(shown["result"])


async def test_a_request_under_way_when_the_server_restarts_is_marked_partial_keeps_its_finished_steps_and_is_not_started_again(asker):
    client, user, ws, h = asker
    finished = {"status": "waiting", "steps": [{"label": "One", "state": "done"}, {"label": "Two", "state": "pending"}]}
    run_id = await _running_run(ws, user, _free_read().name, result=finished, compose_run_id="c1")
    waiting = await _running_run(ws, user, _free_read().name)
    await runs.runs().update_one({"id": waiting}, {"$set": {"status": "ready"}})                         # not under way: must be left alone
    assert await runs.reconcile_after_restart() >= 1
    row = await runs.runs().find_one({"id": run_id})
    assert row["status"] == "partial" and row["error"]["code"] == "interrupted" and "restarted" in row["error"]["message"]
    assert row["result"]["steps"][0]["state"] == "done"                                                  # the finished step is kept
    assert (await runs.runs().find_one({"id": waiting}))["status"] == "ready"
    assert await runs.reconcile_after_restart() == 0                                                     # a second sweep finds nothing to change
    shown = (await client.get(f"/api/v1/ask/{run_id}", headers=h)).json()
    assert shown["status"] == "partial" and shown["error"]["code"] == "interrupted"
    again = await client.post(f"/api/v1/ask/{run_id}/confirm", headers=h)
    assert again.status_code == 409                                                                      # it is not offered for a second go


async def test_fixed_sentences_and_step_summaries_are_given_in_the_members_language_and_fall_back_to_english(monkeypatch):
    from app.shared import localized_strings

    asked: list[tuple[str, str]] = []

    async def fake(key, language, template, ctx=None):
        asked.append((key, language))
        return f"[{language}] {template}"

    monkeypatch.setattr(localized_strings, "get_localized_string", fake)
    rows = [{"tool": "library.list", "summary": "Lists your saved drafts."}, {"tool": "x.none", "summary": ""}]
    await runs.localize_steps(rows, "ta")
    assert rows[0]["summary_local"] == "[ta] Lists your saved drafts." and "summary_local" not in rows[1]
    assert ("ask.tool.library.list", "ta") in asked
    assert await runs.localize_fixed("ask.message.not_understood", runs.MESSAGES["none"], "hi") == f"[hi] {runs.MESSAGES['none']}"

    asked.clear()
    english = [{"tool": "library.list", "summary": "Lists your saved drafts."}]
    await runs.localize_steps(english, "en")
    assert "summary_local" not in english[0] and asked == []                                             # English asks nothing
    assert await runs.localize_fixed("k", "Stopped.", "") == "Stopped."

    async def broken(key, language, template, ctx=None):
        raise RuntimeError("translator down")

    monkeypatch.setattr(localized_strings, "get_localized_string", broken)
    assert await runs.localize_fixed("k", "Stopped.", "ta") == "Stopped."                                # a failure never hides the English
