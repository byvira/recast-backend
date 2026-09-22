"""Member-scoped TTS/acoustic settings — the data layer behind Remy's
"Narration Voice" tab. Scaffolding only: this model + its CRUD routes persist
real settings, but nothing actually synthesizes audio from them yet — no TTS
engine is wired in. That's separate, later work (the audio pipeline itself is
still a marketing page, per docs/STATUS.md's FLOW_AUDIT).
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class MemberVoiceSettings(BaseModel):
    id: str                       # f"{workspace_id}:{user_id}"
    workspace_id: str
    user_id: str

    # Opaque catalog id — which real voice catalog backs this (built-in TTS
    # engine, a cloned voice, etc.) is undecided; kept opaque like
    # Platform.text_enum_value so the catalog can be built later without a
    # migration here.
    tts_voice: str = "piper_lessac"
    pitch_shift_semitones: float = 0.0        # -6..6
    speech_speed: float = 1.0                 # 0.8..1.5
    vocal_energy: int = 80                    # 0..100
    pause_cadence: str = "natural"             # tight | natural | dramatic
    emotional_tone: str = "conversational"     # authoritative | conversational | visionary | tactical
    voice_locked: bool = True

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class MemberVoiceSettingsWrite(BaseModel):
    tts_voice: Optional[str] = None
    pitch_shift_semitones: Optional[float] = Field(None, ge=-6, le=6)
    speech_speed: Optional[float] = Field(None, ge=0.8, le=1.5)
    vocal_energy: Optional[int] = Field(None, ge=0, le=100)
    pause_cadence: Optional[str] = None
    emotional_tone: Optional[str] = None
    voice_locked: Optional[bool] = None
