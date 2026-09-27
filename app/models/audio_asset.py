"""AudioAsset — one audio project (a narration, a multi-voice dialogue, an
uploaded-and-enhanced recording). Same ContentPiece/ContentPieceVersion-
derived shape as ImageAsset (app.models.image_asset) — see
pow/audio_image_pipeline/02-audio-pipeline-plan.md for the full plan.

Show is a generic organizing container, not podcast-specific — decided
2026-09-25 during scoping: a user doesn't have to be making a podcast to
want recurring episodes/pieces grouped under one umbrella. Podcast
distribution (pow/audio_image_pipeline/05) is one consumer of Show, not
its definition.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class AudioSourceType(str, Enum):
    SCRIPT_TTS = "script_tts"  # text script synthesized via TTS
    DIALOGUE = "dialogue"      # multi-voice, multiple turns
    UPLOADED = "uploaded"      # raw upload/recording, transcribed+enhanced
    RSS_IMPORT = "rss_import"


class AudioApprovalStatus(str, Enum):  # mirrors ImageApprovalStatus
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TranscriptWord(BaseModel):
    word: str
    start_s: float
    end_s: float
    speaker: Optional[str] = None  # for diarization


class AudioAsset(BaseModel):
    id: str
    workspace_id: str
    # Not in the plan's original model sketch — added 2026-09-26, same
    # reason as ImageAsset.brand_id (see PROGRESS.md Decisions Log): every
    # other generation surface requires an explicit brand_id, no "default
    # brand" concept exists anywhere in this codebase.
    brand_id: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    title: str
    source_type: AudioSourceType
    script: Optional[str] = None                    # for TTS-sourced audio
    voice_settings_snapshot: Optional[dict] = None   # copied from MemberVoiceSettings at generation time
    media_id: Optional[str] = None                   # -> MediaAsset, the rendered/processed audio file
    transcript: list[TranscriptWord] = []
    dsp_settings: dict = {}                           # denoise/de-ess/compressor/EQ toggles actually applied
    approval_status: AudioApprovalStatus = AudioApprovalStatus.PENDING
    approved_master_media_id: Optional[str] = None
    # Added 2026-09-26 (governance sweep) — mirrors ImageAsset.version_count
    # exactly; needed once approve/reject/versions/restore endpoints exist.
    version_count: int = 1
    source_piece_id: Optional[str] = None             # repurpose-flow linkage
    source_content_hash: Optional[str] = None
    show_id: Optional[str] = None
    qa_flagged: bool = False
    qa_flag_reason: Optional[str] = None
    # Added 2026-09-26 (localization slice, bugs/gaps sweep) — per
    # PROGRESS.md's Decisions Log: "per-language AudioAsset variants
    # (source_audio_asset_id + language fields) with their own approval
    # gate." language defaults to "en" so every already-created real
    # AudioAsset is unaffected; source_audio_asset_id stays unset unless
    # this asset was produced by /localize from another one.
    language: str = "en"
    source_audio_asset_id: Optional[str] = None


class AudioAssetVersion(BaseModel):  # mirrors ImageAssetVersion
    version_id: str
    audio_asset_id: str
    workspace_id: str
    user_id: str
    version_number: int
    script_snapshot: Optional[str] = None
    # Added 2026-09-26 (governance sweep) — without this, restoring a
    # version could only ever snapshot the script text, never which
    # actual rendered/enhanced file was active at that point; mirrors how
    # ImageAssetVersion.slides_snapshot already carries real media_id
    # references, not just text.
    media_id: Optional[str] = None
    action: str
    created_at: datetime


class AudioShareLink(BaseModel):  # mirrors ImageShareLink
    token: str
    audio_asset_id: str
    workspace_id: str
    created_by: str
    created_at: datetime
    expires_at: datetime
    revoked: bool = False


class GuestVoiceProfile(BaseModel):
    """A non-member speaker's voice for one dialogue AudioAsset — scoped
    per-episode, not workspace-wide like MemberVoiceSettings (decided in
    PROGRESS.md's Decisions Log: "guests aren't members," so this can't
    reuse that schema). `voice_id` is a real provider voice reference
    (ElevenLabs voice_id or a Deepgram Aura-2 model name), the same
    tts_voice shape MemberVoiceSettings already uses — synthesize_speech
    doesn't need a second code path, just a MemberVoiceSettings
    constructed with this voice_id at call time."""

    id: str
    audio_asset_id: str
    workspace_id: str
    name: str
    voice_id: str
    created_at: datetime


class ShowType(str, Enum):
    PODCAST = "podcast"
    SERIES = "series"  # any other recurring/grouped content, audio or otherwise
    OTHER = "other"


class Show(BaseModel):
    """Groups related AudioAssets under one named container. Not
    podcast-specific — podcast distribution is one real use of it, not its
    whole purpose (decided 2026-09-25)."""

    id: str
    workspace_id: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    title: str
    description: Optional[str] = None
    show_type: ShowType = ShowType.OTHER
    cover_art_media_id: Optional[str] = None
    # Only populated when show_type == PODCAST and the real distribution
    # path (pow/audio_image_pipeline/05) is actually in use — kept
    # optional so a non-podcast Show never carries irrelevant fields.
    podcast_category: Optional[str] = None
    rss_feed_settings: Optional[dict] = None
