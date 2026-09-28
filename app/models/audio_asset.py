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


class SignoffRole(str, Enum):
    """The 4 review roles the Audio Inspector's Governance panel already
    names in its UI — real signoffs against these, not a decorative
    checklist with nothing behind it."""
    AUDIO_ENGINEER = "audio_engineer"
    BRAND_GUARDIAN = "brand_guardian"
    EXECUTIVE_PRODUCER = "executive_producer"
    LEGAL_COMPLIANCE = "legal_compliance"


class Signoff(BaseModel):
    role: SignoffRole
    user_id: str
    user_name: str
    signed_at: datetime


class Assembly(BaseModel):
    id: str
    media_id: str
    components: dict = {}          # what went into the mix, as reported by the mixer
    # The source recording's transcript, shifted for whatever was added before
    # or spliced into it (intro, sponsor read). Empty if the source had none.
    transcript: list[TranscriptWord] = []
    created_by: str
    created_at: datetime


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
    # The file exactly as it was uploaded, kept when cleanup changed it so the
    # A/B comparison can play the real "before". None for anything that was
    # never cleaned up (generated audio, or uploads cleanup didn't apply to).
    original_media_id: Optional[str] = None
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
    # Real Governance & Multi-Tier Review Matrix — the UI already named
    # these 4 roles with nothing behind the checkboxes; each entry is one
    # real member's real signoff, not a locally-toggled checkbox.
    signoffs: list[Signoff] = []
    # Finished episodes built from this recording (intro, sponsor read, music,
    # outro). Newest last. The recording itself is never changed by these.
    assemblies: list[Assembly] = []
    # Real videos rendered from this recording (a clip, or the whole
    # episode), newest last.
    video_clips: list["VideoClip"] = []


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
    # The word timings that matched this version's audio. Cleanup that cuts
    # time moves the words, so restoring an earlier version has to bring its
    # transcript back too. None on versions made before this existed.
    transcript_snapshot: Optional[list[dict]] = None
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
    # Off by default: a link only takes guest feedback if its owner turns
    # this on for it specifically. hold_for_approval keeps a guest note out
    # of the owner's view of the recording until they've reviewed it.
    allow_comments: bool = False
    hold_for_approval: bool = True


class PodcastFeedSettings(BaseModel):
    """One real, public RSS feed per brand. Real-world podcast platforms
    (Spotify for Podcasters, Apple Podcasts Connect) don't offer a push
    API for a third-party host to dispatch episodes into — they poll a
    feed URL you submit once. This is that feed, not a "connected"
    integration."""

    id: str  # == brand_id
    workspace_id: str
    brand_id: str
    token: str
    title: str
    description: str = ""
    is_enabled: bool = True
    # Spotify for Podcasters and Apple Podcasts Connect both reject a feed
    # missing these — real requirements, not decoration.
    language: str = "en"
    category: str = ""             # one of PODCAST_CATEGORIES
    explicit: bool = False
    author_name: str = ""
    owner_email: str = ""
    cover_media_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# Apple Podcasts' own top-level category list (podcasts.apple.com's real
# taxonomy) — validated against, not invented.
PODCAST_CATEGORIES = [
    "Arts", "Business", "Comedy", "Education", "Fiction", "Government",
    "Health & Fitness", "History", "Kids & Family", "Leisure", "Music",
    "News", "Religion & Spirituality", "Science", "Society & Culture",
    "Sports", "Technology", "True Crime", "TV & Film",
]


class PodcastFeedEnableRequest(BaseModel):
    brand_id: str
    title: str
    description: str = ""


class PodcastFeedUpdateRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    is_enabled: Optional[bool] = None
    language: Optional[str] = None
    category: Optional[str] = None
    explicit: Optional[bool] = None
    author_name: Optional[str] = None
    owner_email: Optional[str] = None
    cover_media_id: Optional[str] = None


class PodcastFeedChecklistItem(BaseModel):
    label: str
    ok: bool
    hint: str = ""


class PodcastFeedStatusResponse(BaseModel):
    is_enabled: bool
    token: Optional[str] = None
    feed_url: Optional[str] = None
    title: str = ""
    description: str = ""
    episode_count: int = 0
    language: str = "en"
    category: str = ""
    explicit: bool = False
    author_name: str = ""
    owner_email: str = ""
    cover_url: Optional[str] = None
    # Real conditions, checked live — not a static "you're all set" banner.
    checklist: list[PodcastFeedChecklistItem] = []


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


class AudioComment(BaseModel):
    """A review note pinned to a moment in one recording — from a team
    member, or (is_guest=True) a listener on a shared link with comments
    turned on. A guest note starts unapproved when its link holds guests
    for approval; approved defaults true for a team member's own note,
    which was never gated on anything."""
    id: str
    audio_asset_id: str
    workspace_id: str
    user_id: str
    user_name: str
    time_s: float
    text: str
    created_at: datetime
    resolved: bool = False
    is_guest: bool = False
    approved: bool = True


class AudioCommentCreate(BaseModel):
    time_s: float
    text: str


class GuestCommentCreate(BaseModel):
    time_s: float
    text: str
    guest_name: str = ""
    # A field real visitors never see or fill in; a bot that fills every
    # field on the form will fill this one too. Any value here means the
    # submission is discarded, but told it "succeeded" regardless, so a
    # bot never learns which field gave it away.
    website: str = ""
    turnstile_token: str = ""


class AudioCommentUpdate(BaseModel):
    resolved: Optional[bool] = None
    approved: Optional[bool] = None


class KitClip(BaseModel):
    media_id: str
    name: str


class MusicBed(BaseModel):
    id: str
    name: str
    media_id: str


class MusicLibraryTrack(BaseModel):
    """A curated, CC0-licensed track ingested once (app.pipelines.media.
    music_library) and offered to every workspace's Music tab alongside its
    own uploaded beds — real audio, real license, not workspace-owned."""
    id: str
    name: str
    media_id: str
    artist: str
    source: str = "jamendo"
    source_track_id: str
    license_name: str
    license_url: str
    created_at: datetime


class AudioKit(BaseModel):
    """A brand's reusable episode pieces: bumpers, a sponsor read, music
    beds, a transition sound for real pauses, and a brand signature clip
    played at the very start."""
    brand_id: str
    workspace_id: str
    intro: Optional[KitClip] = None
    outro: Optional[KitClip] = None
    sponsor_clip: Optional[KitClip] = None
    sponsor_name: str = ""
    sponsor_script: str = ""
    music_beds: list[MusicBed] = []
    transition_sfx: Optional[KitClip] = None
    brand_signature: Optional[KitClip] = None
    updated_at: Optional[datetime] = None


class KitClipOut(KitClip):
    url: str


class MusicBedOut(MusicBed):
    url: str
    is_library: bool = False
    artist: Optional[str] = None


class AudioKitOut(BaseModel):
    brand_id: str
    intro: Optional[KitClipOut] = None
    outro: Optional[KitClipOut] = None
    sponsor_clip: Optional[KitClipOut] = None
    sponsor_name: str = ""
    sponsor_script: str = ""
    music_beds: list[MusicBedOut] = []
    transition_sfx: Optional[KitClipOut] = None
    brand_signature: Optional[KitClipOut] = None


class AudioKitUpdate(BaseModel):
    """Only the fields sent are changed. Sending a clip as null clears it."""
    intro: Optional[KitClip] = None
    outro: Optional[KitClip] = None
    sponsor_clip: Optional[KitClip] = None
    sponsor_name: Optional[str] = None
    sponsor_script: Optional[str] = None
    transition_sfx: Optional[KitClip] = None
    # A real 5-second cap, enforced where this is set (the media's own
    # duration_s) — a "signature" any longer isn't one.
    brand_signature: Optional[KitClip] = None


class MusicBedCreate(BaseModel):
    name: str
    media_id: str


class VideoClip(BaseModel):
    id: str
    media_id: str
    start_s: float
    end_s: float
    style: str   # cover | solid | waveform | cover_wave
    size: str    # square | vertical | landscape
    title: str = ""
    created_by: str
    created_at: datetime


class MakeVideoRequest(BaseModel):
    start_s: float = 0.0
    end_s: Optional[float] = None   # None = the whole recording
    style: str = "cover"
    size: str = "square"
    title: Optional[str] = None


class SuggestedClip(BaseModel):
    start_s: float
    end_s: float
    quote: str
    reason: str


class SuggestClipsResponse(BaseModel):
    # Always labelled as suggestions, never applied automatically.
    suggestions: list[SuggestedClip]


class SoundbiteStatus(str, Enum):
    READY = "ready"
    NEEDS_ATTENTION = "needs_attention"


class Soundbite(BaseModel):
    """A real, extracted short clip from an AudioAsset's real master audio —
    the Batch Approval Queue's real backing data (was mock-only). Quality
    status/confidence are real, measured values, not an AI-labeled guess:
    derived from actual peak/loudness checks on the trimmed audio itself."""
    id: str
    audio_asset_id: str
    workspace_id: str
    media_id: str
    quote: str
    reason: str
    start_s: float
    end_s: float
    duration_s: float
    status: SoundbiteStatus
    flag_message: Optional[str] = None
    confidence: int  # 0-100, derived from real measured issues, see soundbite_extraction.py
    measured_lufs: Optional[float] = None
    approval_status: AudioApprovalStatus = AudioApprovalStatus.PENDING
    created_by: str
    created_at: datetime


class SoundbiteOut(Soundbite):
    url: str
