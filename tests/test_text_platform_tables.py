"""The text rule tables come from the platform list, and platform names map to registry keys without lowercasing display text."""
import pytest

from app.models.text import Platform
from app.pipelines.publish.spine import platform_key
from app.pipelines.text import generator


def test_every_text_platform_has_a_rule_a_hashtag_rule_and_a_call_to_action_rule():
    members = list(Platform)
    assert len(members) >= 8
    for table_name in ("PLATFORM_RULES", "HASHTAG_RULES", "CTA_RULES"):
        table = getattr(generator, table_name)
        missing = [m.value for m in members if not (table.get(m) or "").strip()]
        assert not missing, f"{table_name} has nothing for {missing}"


@pytest.mark.parametrize("display,key", [
    ("LinkedIn", "linkedin"),
    ("Twitter/X", "twitter"),
    ("Twitter/X Thread", "twitter"),
    ("Instagram", "instagram"),
    ("YouTube", "youtube"),
    ("", ""),
    (None, ""),
])
def test_a_pieces_platform_text_maps_to_the_registry_key(display, key):
    assert platform_key(display) == key


def test_a_name_the_registry_does_not_know_still_falls_back_to_lower_case():
    assert platform_key("Some New Place") == "some new place"


@pytest.mark.parametrize("name", [
    "text/generate/approved_copy", "text/generate/approved_vocabulary", "text/generate/banned_words",
    "text/generate/engagement_patterns", "text/generate/english_terms", "text/generate/hashtag_banned",
    "text/generate/hashtag_none", "text/generate/specificity", "text/generate/language_instruction",
])
def test_the_goal_tone_engagement_and_hashtag_wording_lives_in_prompt_files(name):
    from app.prompts.registry import _env

    source = _env.loader.get_source(_env, f"{name}.jinja")[0]
    assert source.strip()
    _env.parse(source)  # a prompt file with broken template syntax would fail here
