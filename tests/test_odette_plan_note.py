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
