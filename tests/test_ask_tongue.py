"""Which language and script Ask answers in, and that a reply is written in it rather than translated. No model or network is used."""
from app.ask import pipeline, tongue


def test_a_short_follow_up_keeps_the_language_the_conversation_was_already_in():
    assert tongue.choose("ok do it", "en", "ta", "latin") == ("ta", "latin")
    assert tongue.choose("seri pannu", None, "ta", "latin") == ("ta", "latin")
    assert tongue.choose("yes", "en", "hi", "native") == ("hi", "native")


def test_a_long_message_in_english_moves_the_conversation_back_to_english():
    text = "Please write three posts about our new shop opening for the weekend and keep them friendly"
    assert tongue.choose(text, "en", "ta", "latin") == ("en", "latin")


def test_a_short_message_the_detector_places_elsewhere_does_not_change_the_conversation():
    assert tongue.choose("post pannunga", "id", "ta", "latin") == ("ta", "latin")          # a detector slip on romanised Tamil
    assert tongue.choose("Bonjour, fais-moi un post sur notre nouveau produit", "fr", "ta", "latin")[0] == "fr"   # a real, longer change is followed


def test_the_letters_a_person_uses_decide_the_script_and_the_language():
    assert tongue.choose("எனக்கு ஒரு பதிவு எழுது", None, "", "") == ("ta", "native")
    assert tongue.choose("मुझे एक पोस्ट चाहिए", "hi", "ta", "latin") == ("hi", "native")


def test_with_nothing_else_to_go_on_the_members_own_default_is_used_and_english_is_left_alone():
    assert tongue.choose("hello", None, "", "", default="ta") == ("ta", "latin")
    assert tongue.choose("hello", None, "", "", default="en") == ("", "latin")
    assert tongue.choose("hello", None, "", "") == ("", "latin")


def test_the_instruction_to_the_model_names_the_language_and_the_script_or_is_empty_for_english():
    assert tongue.rule("en", "latin") == "" and tongue.rule("", "latin") == ""
    assert "English letters" in tongue.rule("ta", "latin") and "own script" in tongue.rule("ta", "native")
    system = pipeline.decide_prompt("seri", [], "ta", "", True, "latin")[1]
    assert "English letters" in system and system.count('language with code "ta"') >= 1


async def test_a_reply_is_written_by_the_model_in_the_language_and_falls_back_when_it_cannot():
    seen: list[tuple[str, str]] = []

    async def think(role, prompt, system):
        seen.append((role, prompt))
        return {"text": "Seri, 2 step la mudichidalaam. Idhu ilavasam."}

    out = await tongue.say("I can do this in 2 steps. It is free.", "ta", "latin", think)
    assert out == "Seri, 2 step la mudichidalaam. Idhu ilavasam." and "English letters" in seen[0][1] and "2 steps" in seen[0][1]
    assert await tongue.say("x", "en", "latin", think) is None and len(seen) == 1                     # English asks nothing

    async def broken(role, prompt, system):
        raise RuntimeError("model down")

    assert await tongue.say("x", "ta", "native", broken) is None                                       # the caller then uses its fixed sentence
