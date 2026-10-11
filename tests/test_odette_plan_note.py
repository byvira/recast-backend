from app.shared.tier_policy import mentions_team_topics, plan_note, policy_for


def test_a_workspace_of_one_is_told_not_to_recommend_team_things():
    note = plan_note(policy_for({"tier": "single"}))
    assert "exactly one person" in note and "roles" in note


def test_a_pair_is_told_to_keep_it_simple_and_a_team_gets_no_note():
    assert "two people" in plan_note(policy_for({"tier": "duo"}))
    assert plan_note(policy_for({"tier": "large"})) == ""


def test_team_talk_is_recognised_and_plain_advice_is_not():
    assert mentions_team_topics("Add a seat for your new member")
    assert mentions_team_topics("Review who has admin permissions")
    assert not mentions_team_topics("Post on LinkedIn on Tuesday mornings, your reach is highest then")
    assert not mentions_team_topics(None, "", "Your hooks are getting stronger")


async def test_a_filtered_team_insight_never_lends_its_priority_to_a_kept_one(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from app.agents.supervisor import nodes

    monkeypatch.setattr(nodes, "workspace_policy", AsyncMock(return_value={"team_rules": False}))
    monkeypatch.setattr(nodes, "workspace_insights", MagicMock(insert_one=AsyncMock()))
    monkeypatch.setattr(nodes, "workspace_flags", MagicMock(find_one=AsyncMock(return_value=None), insert_one=AsyncMock(), update_one=AsyncMock()))
    monkeypatch.setattr(nodes, "agent_worker_state", MagicMock(update_one=AsyncMock()))
    monkeypatch.setattr(nodes, "_project_insight", AsyncMock())
    deliver = AsyncMock(return_value="n1")
    monkeypatch.setattr(nodes.notify_mod, "deliver", deliver)

    state = {
        "workspace_id": "w1", "events": [], "signals": [], "digest": {},
        "findings": {
            "notify": True,
            "insights": [
                {"title": "Add a seat for your new member", "body": "Invite your team", "priority": "high"},
                {"title": "Post on Tuesday mornings", "body": "Your reach is highest then", "priority": "high"},
            ],
            "flags": [],
        },
    }
    out = await nodes.persist_node(state)
    assert len(out["persisted"]["insight_ids"]) == 1
    titles = [call.kwargs["title"] for call in deliver.call_args_list]
    assert titles == ["Post on Tuesday mornings"], "the notification carries the kept insight, not the filtered one"


def _persist_mocks(monkeypatch, policy):
    from unittest.mock import AsyncMock, MagicMock

    from app.agents.supervisor import nodes

    monkeypatch.setattr(nodes, "workspace_policy", AsyncMock(return_value=policy))
    insights = MagicMock(insert_one=AsyncMock())
    flags = MagicMock(find_one=AsyncMock(return_value=None), insert_one=AsyncMock(), update_one=AsyncMock())
    monkeypatch.setattr(nodes, "workspace_insights", insights)
    monkeypatch.setattr(nodes, "workspace_flags", flags)
    monkeypatch.setattr(nodes, "agent_worker_state", MagicMock(update_one=AsyncMock()))
    monkeypatch.setattr(nodes, "_project_insight", AsyncMock())
    monkeypatch.setattr(nodes, "_project_flag", AsyncMock())
    monkeypatch.setattr(nodes.notify_mod, "deliver", AsyncMock(return_value="n1"))
    return nodes, insights, flags


async def test_a_flag_about_team_matters_is_dropped_for_one_person_and_cohort_talk_for_a_pair(monkeypatch):
    nodes, _, flags = _persist_mocks(monkeypatch, {"team_rules": False, "cohorts": False})
    state = {
        "workspace_id": "w1", "events": [], "signals": [], "digest": {},
        "findings": {"notify": False, "insights": [], "flags": [{"severity": "warning", "summary": "Members are skipping approvals"}]},
    }
    await nodes.persist_node(state)
    assert flags.insert_one.await_count == 0

    nodes, _, flags = _persist_mocks(monkeypatch, {"team_rules": True, "cohorts": False})
    state["findings"]["flags"] = [{"severity": "warning", "summary": "Set up a cohort review chain"}]
    await nodes.persist_node(state)
    assert flags.insert_one.await_count == 0


async def test_an_insight_with_a_made_up_priority_or_kind_is_saved_with_safe_values(monkeypatch):
    nodes, insights, _ = _persist_mocks(monkeypatch, {"team_rules": True, "cohorts": True})
    state = {
        "workspace_id": "w1", "events": [], "signals": [], "digest": {},
        "findings": {"notify": False, "flags": [], "insights": [{"title": "Post on Tuesdays", "body": "b", "priority": "urgent!!", "kind": "alarm"}]},
    }
    await nodes.persist_node(state)
    saved = insights.insert_one.await_args.args[0]
    assert saved["priority"] == "medium" and saved["kind"] == "recommendation"


async def test_odette_still_briefs_through_the_backup_models_when_the_main_one_is_down(monkeypatch):
    from unittest.mock import AsyncMock

    from app.agents.supervisor import nodes
    from app.shared import llm

    monkeypatch.setattr(nodes, "_groq_chat", AsyncMock(side_effect=RuntimeError("provider down")))
    backup = AsyncMock(return_value="backup reply")
    monkeypatch.setattr(llm, "call_llm_fallback", backup)
    reply = await nodes._synth_reply(object(), [{"role": "user", "content": "DIGEST"}], "w1")
    assert reply == "backup reply" and backup.await_count == 1
