from app.agents.supervisor.personas import odette_flag_summary


async def test_flag_wording_follows_the_plan():
    solo = await odette_flag_summary("brand_voice_instability", {"count": 4, "tier": "single"})
    pair = await odette_flag_summary("brand_voice_instability", {"count": 4, "tier": "duo"})
    team = await odette_flag_summary("brand_voice_instability", {"count": 4, "tier": "large"})
    assert "You changed your brand voice" in solo and "sound different from each other" in solo
    assert "both of your posts" in pair
    assert "everyone's posts" in team

    cap_solo = await odette_flag_summary("daily_publish_cap", {"count": 12, "cap": 10, "tier": "single"})
    cap_team = await odette_flag_summary("daily_publish_cap", {"count": 40, "cap": 30, "tier": "large"})
    # an alert line, not a limit: the wording must not claim a cap or push a bigger plan while nothing is blocked
    assert "You've published 12 times" in cap_solo and "Nothing was blocked" in cap_solo
    assert "bigger plan" not in cap_solo and "allows" not in cap_team
    assert "large plan" in cap_team


def test_no_wording_file_contains_an_em_dash_in_its_text():
    from pathlib import Path

    folder = Path(__file__).resolve().parent.parent / "app" / "prompts" / "localized"
    hits = []
    for path in folder.glob("*.yaml"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.lstrip().startswith("#") and "\u2014" in line:
                hits.append(f"{path.name}:{number}")
    assert hits == []


async def test_an_unknown_flag_is_said_in_plain_words_and_a_pair_has_its_own_publish_cap_wording():
    from app.agents.supervisor.personas import odette_flag_summary

    unknown = await odette_flag_summary("something_new", {"count": 3, "nested": {"a": 1}})
    assert "{" not in unknown and "nested" not in unknown and "count 3" in unknown
    pair = await odette_flag_summary("daily_publish_cap", {"count": 9, "cap": 5, "tier": "duo"})
    solo = await odette_flag_summary("daily_publish_cap", {"count": 9, "cap": 5, "tier": "single"})
    assert "two of you" in pair and "two of you" not in solo
