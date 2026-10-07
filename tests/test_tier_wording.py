from app.agents.supervisor.personas import odette_flag_summary


async def test_flag_wording_follows_the_plan():
    solo = await odette_flag_summary("brand_voice_instability", {"count": 4, "tier": "single"})
    pair = await odette_flag_summary("brand_voice_instability", {"count": 4, "tier": "duo"})
    team = await odette_flag_summary("brand_voice_instability", {"count": 4, "tier": "large"})
    assert "Your brand voice" in solo and "everything you post" in solo
    assert "both of your outputs" in pair
    assert "every member's output" in team

    cap_solo = await odette_flag_summary("daily_publish_cap", {"count": 12, "cap": 10, "tier": "single"})
    cap_team = await odette_flag_summary("daily_publish_cap", {"count": 40, "cap": 30, "tier": "duo"})
    assert "You published 12 times" in cap_solo and "move up a plan" in cap_solo
    assert "on the duo plan" in cap_team
