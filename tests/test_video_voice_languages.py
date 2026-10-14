"""The standard voice for each language: a script is spoken by a voice made for its language, and a member's own voice is kept
only when it reads that language."""
import pytest

from app.models.voice_settings import MemberVoiceSettings
from app.pipelines.media import tts_generation as T


def _vs(voice: str = "piper_lessac") -> MemberVoiceSettings:
    return MemberVoiceSettings(id="x", workspace_id="w", user_id="u", tts_voice=voice)


@pytest.mark.parametrize("code,expected", [
    ("en", "aura-2-thalia-en"), ("es", "aura-2-celeste-es"), ("fr", "aura-2-agathe-fr"), ("de", "aura-2-viktoria-de"),
    ("it", "aura-2-livia-it"), ("nl", "aura-2-rhea-nl"), ("ja", "aura-2-izanami-ja"), ("es-MX", "aura-2-celeste-es"),
])
def test_a_script_is_spoken_by_the_voice_made_for_its_language(code, expected):
    assert T._resolve_deepgram_model(_vs(), code) == expected


def test_a_members_own_voice_is_kept_only_for_the_language_it_reads():
    assert T._resolve_deepgram_model(_vs("aura-2-apollo-en"), "en") == "aura-2-apollo-en"
    assert T._resolve_deepgram_model(_vs("aura-2-apollo-en"), "fr") == "aura-2-agathe-fr"
    assert T._resolve_deepgram_model(_vs("aura-2-hector-fr"), "fr") == "aura-2-hector-fr"


def test_with_no_language_stated_the_behaviour_is_what_it_was():
    assert T._resolve_deepgram_model(_vs()) == "aura-2-thalia-en"
    assert T._resolve_deepgram_model(_vs("aura-2-apollo-en")) == "aura-2-apollo-en"
    assert T._resolve_deepgram_model(_vs("some-elevenlabs-id")) == "aura-2-thalia-en"
    assert T._resolve_deepgram_model(_vs(), "ta") == "aura-2-thalia-en"            # not a Deepgram language: refused before this point
