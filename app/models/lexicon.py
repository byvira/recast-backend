"""Member-scoped lexicon — pronunciation dictionary, jargon whitelist/
blacklist, and writing-blueprint thresholds. Backs Remy's "Vocabulary &
Pronunciation" tab. Scaffolding: persists real data, but nothing enforces
the writing-blueprint thresholds or feeds the pronunciation dictionary into
a TTS engine yet — that wiring is separate, later work.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class PronunciationEntry(BaseModel):
    id: str
    term: str
    # Despite the field name (kept for backward compatibility with the
    # existing UI/API contract), this is used as a plain-text phonetic
    # RESPELLING (e.g. "zen-dlee"), not strict IPA — ElevenLabs' alias-type
    # pronunciation rule (the only mechanism that works with the
    # eleven_multilingual_v2 model this codebase synthesizes with) does a
    # literal text substitution, not phoneme interpretation. Real IPA
    # characters here would be read aloud as-is, which is wrong. See
    # tts_generation.py::sync_pronunciation_dictionary.
    ipa: str
    notes: str = ""


class WritingBlueprint(BaseModel):
    """Mirrors the "Writing Blueprint Settings" state already declared (but
    never rendered) in the old remy/page.tsx — real fields now, dead
    frontend state to be wired up separately."""
    sentence_max_words: int = 22
    rhetorical_density: int = 16               # rhetorical device count, per 1000 words
    anecdote_frequency: str = "1 per 400 words"
    drift_threshold_pct: int = 15


class MemberLexicon(BaseModel):
    id: str                       # f"{workspace_id}:{user_id}"
    workspace_id: str
    user_id: str

    pronunciations: list[PronunciationEntry] = Field(default_factory=list)
    whitelist: list[str] = Field(default_factory=list)
    blacklist: list[str] = Field(default_factory=list)
    writing_blueprint: WritingBlueprint = Field(default_factory=WritingBlueprint)

    # Real ElevenLabs pronunciation-dictionary sync target, set once the
    # first save with at least one pronunciation succeeds. None means
    # "never synced yet" or "last sync failed" — synthesize_speech simply
    # skips pronunciation locators in that case, never blocks narration.
    elevenlabs_dictionary_id: Optional[str] = None
    elevenlabs_dictionary_version_id: Optional[str] = None

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class MemberLexiconWrite(BaseModel):
    """Full-document replace, matching the frontend's existing client-side
    array-manipulation pattern (add/remove a pronunciation, then save the
    whole list) — simplest contract for a scaffolding pass; per-entry
    PATCH endpoints can follow if the UI ever needs finer-grained saves."""
    pronunciations: list[PronunciationEntry] = Field(default_factory=list)
    whitelist: list[str] = Field(default_factory=list)
    blacklist: list[str] = Field(default_factory=list)
    writing_blueprint: WritingBlueprint = Field(default_factory=WritingBlueprint)
