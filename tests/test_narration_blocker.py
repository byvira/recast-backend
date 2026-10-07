from app.core.config import settings
from app.models.voice_settings import MemberVoiceSettings
from app.pipelines.media import tts_generation as tts


def _voice(voice: str) -> MemberVoiceSettings:
    return MemberVoiceSettings(id="w:u", workspace_id="w", user_id="u", tts_voice=voice)


def test_standard_voice_languages_are_never_blocked(monkeypatch):
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "key")
    assert tts.narration_blocker("English", _voice("piper_lessac")) is None
    assert tts.narration_blocker("Spanish", _voice("piper_lessac")) is None


def test_tamil_with_the_offline_voice_says_to_pick_a_premium_voice(monkeypatch):
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "key")
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "key")
    monkeypatch.setattr(settings, "ELEVENLABS_ENABLED", True)
    reason = tts.narration_blocker("Tamil", _voice("piper_lessac"))
    assert reason and "premium voice" in reason and "Tamil" in reason


def test_tamil_without_the_premium_service_says_it_is_not_switched_on(monkeypatch):
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "key")
    monkeypatch.setattr(settings, "ELEVENLABS_ENABLED", False)
    reason = tts.narration_blocker("Tamil", _voice("abc123"))
    assert reason and "isn't switched on" in reason


def test_tamil_with_a_real_voice_is_allowed(monkeypatch):
    monkeypatch.setattr(settings, "DEEPGRAM_API_KEY", "key")
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "key")
    monkeypatch.setattr(settings, "ELEVENLABS_ENABLED", True)
    monkeypatch.setattr(tts, "_elevenlabs_skip_until", 0.0)
    assert tts.narration_blocker("Tamil", _voice("21m00Tcm4TlvDq8ikWAM")) is None
