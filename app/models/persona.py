"""``member_personas`` document schema — one per (workspace_id, user_id).

Storage decision (from the approved plan): a **hybrid** profile.

* The document *grows* — lifetime counters, an all-time topic histogram, and a
  capped drift-history log are never discarded.
* The *active voice baseline* is a bounded-recency EWMA centroid over the
  member's most recent pieces, so drift is judged against a smoothed picture of
  how they write *now*, not their entire history and not one noisy week.

``_id`` is the deterministic compound key ``f"{workspace_id}:{user_id}"`` so
even a bare ``find_one({"_id": ...})`` is tenant + member scoped.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.models.agent_events import ContentRef


class VoiceProfile(BaseModel):
    baseline_embedding: list[float] = Field(default_factory=list)   # Gemini gemini-embedding-001 @ 768-dim, L2-normalised
    baseline_sample_ids: list[ContentRef] = Field(default_factory=list)   # <= 20, most recent
    ewma_lambda: float = 0.9          # provisional — tune once real usage data exists
    refreshed_at: Optional[datetime] = None
    refreshed_after_piece: int = 0    # lifetime.pieces_observed value at last refresh
    recent_similarities: list[float] = Field(default_factory=list)   # last <=20 per-piece cosines, for trend detection


class StyleFingerprint(BaseModel):
    avg_sentence_len: float = 0.0
    avg_word_len: float = 0.0
    opener_patterns: list[str] = Field(default_factory=list)
    closer_patterns: list[str] = Field(default_factory=list)
    emoji_rate: float = 0.0
    question_rate: float = 0.0
    list_rate: float = 0.0
    reading_grade: float = 0.0


class PersonaLifetime(BaseModel):
    pieces_observed: int = 0
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    by_pipeline: dict[str, int] = Field(default_factory=dict)   # grows generically per PipelineType.value


class PersonaTopics(BaseModel):
    keyword_histogram: dict[str, int] = Field(default_factory=dict)   # all-time
    top_30_window_ids: list[str] = Field(default_factory=list)        # ids of the last 30 pieces


class PersonaVolumeStats(BaseModel):
    daily_counts: dict[str, int] = Field(default_factory=dict)   # {"YYYY-MM-DD": n}, trailing 14 days
    mean: float = 0.0
    stddev: float = 0.0


class PersonaQualityStats(BaseModel):
    trailing_10_flag_rate: float = 0.0
    baseline_flag_rate: float = 0.0


class DriftHistoryEntry(BaseModel):
    at: datetime
    signal_type: str
    similarity: float
    severity: str
    resolved: bool = False


class MemberPersona(BaseModel):
    id: str = Field(alias="_id")            # f"{workspace_id}:{user_id}"
    workspace_id: str
    user_id: str
    persona_name: str = "Remy"             # fixed, member-facing

    lifetime: PersonaLifetime = Field(default_factory=PersonaLifetime)
    voice: VoiceProfile = Field(default_factory=VoiceProfile)
    style_fingerprint: StyleFingerprint = Field(default_factory=StyleFingerprint)
    topics: PersonaTopics = Field(default_factory=PersonaTopics)
    volume_stats: PersonaVolumeStats = Field(default_factory=PersonaVolumeStats)
    quality_stats: PersonaQualityStats = Field(default_factory=PersonaQualityStats)
    drift_history: list[DriftHistoryEntry] = Field(default_factory=list)   # capped at 100 by the writer

    schema_version: int = 1
    created_at: datetime
    updated_at: datetime

    model_config = {"populate_by_name": True}

    @staticmethod
    def make_id(workspace_id: str, user_id: str) -> str:
        return f"{workspace_id}:{user_id}"
