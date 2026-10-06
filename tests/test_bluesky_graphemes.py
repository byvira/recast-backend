from app.pipelines.publish.validators import grapheme_count, validate_bluesky

FAMILY = "\U0001F468\u200D\U0001F469\u200D\U0001F467"


def test_one_family_emoji_is_one_character():
    assert grapheme_count(FAMILY) == 1
    assert len(FAMILY) == 5


def test_accents_and_flags_count_once():
    assert grapheme_count("e\u0301") == 1
    assert grapheme_count("\U0001F1EE\U0001F1F3") == 1


def test_limit_uses_what_a_person_sees():
    ok, issues = validate_bluesky(FAMILY * 300)
    assert ok and not issues
    ok, issues = validate_bluesky(FAMILY * 301)
    assert not ok and "301" in issues[0]
