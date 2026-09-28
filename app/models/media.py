"""MediaAsset — a real, workspace-scoped reference to an image/video/audio
file, stored once and referenced by id everywhere it's used (a piece, a
publish request) rather than re-uploaded per platform.

Part of the hybrid-media plan (see the Ops LLM Health / DEFERRED_AND_PARTIAL_SCOPE
work this session): PublishRequest.media_urls existed as a bare string list
with no type/kind metadata and was never actually populated anywhere — this
is the real contract that replaces it.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class MediaKind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    # A PDF/ZIP export artifact (image-carousel export) — not a playable/
    # viewable media kind, but still a real, downloadable MediaAsset.
    DOCUMENT = "document"


class MediaSource(str, Enum):
    UPLOADED = "uploaded"
    # A brand's own real asset (logo/product shot), matched to a piece's
    # topic rather than newly created — see the default-image picker.
    BRAND_ASSET = "brand_asset"
    # The on-brand "quote card" template render — zero external API calls,
    # the real default for most pieces until/unless AI generation is used.
    GENERATED_TEMPLATE = "generated_template"
    # Real AI generation (Nano Banana / Gemini 2.5 Flash Image today).
    AI_GENERATED = "ai_generated"
    # A trim/crop/aspect-ratio "quick edit" derivative of another MediaAsset
    # (app.pipelines.media.transform) — a real, distinct asset in its own
    # right (own id, own URL), not a mutation of the original, so the
    # source asset stays intact and independently referenceable.
    EDITED = "edited"
    # A server-side Pillow-composited slide (app.pipelines.media.image_render)
    # for an ImageAsset — layout + brand tokens + text baked into a real file,
    # not a browser-only CSS preview.
    RENDERED = "rendered"
    # Real TTS output (app.pipelines.media.tts_generation) for an AudioAsset.
    SYNTHESIZED = "synthesized"
    # Post-DSP-cleanup output (denoise/loudness-normalize/EQ) of another
    # MediaAsset — a real, distinct derivative, same "never mutate the
    # source" convention as EDITED.
    ENHANCED = "enhanced"


class MediaTranscriptWord(BaseModel):
    """One spoken word with its real position in the recording."""

    word: str
    start_s: float
    end_s: float


class MediaChapter(BaseModel):
    """A chapter whose start time is a real position in the recording — never
    a guess: chapters are only ever created from transcript segment starts."""

    start_s: float
    title: str


class MediaAnalysisStatus(str, Enum):
    NONE = "none"        # never analysed
    DONE = "done"
    FAILED = "failed"    # transcription couldn't run; reason in analysis_error


class MediaAsset(BaseModel):
    """Stored in the `media_assets` collection, referenced by id from
    GeneratedPiece.media and PublishRequest — never duplicated per platform
    or per publish attempt."""

    id: str
    workspace_id: str
    kind: MediaKind
    url: str
    mime_type: str
    width: Optional[int] = None
    height: Optional[int] = None
    duration_s: Optional[float] = None
    source: MediaSource
    created_by: str
    created_at: datetime
    # Row 12 — set only for source=AI_GENERATED, by generate_brand_image's
    # post-generation QA gate (a real call_vision check against the brand's
    # VisualIdentity, not a guess). A flagged image is still attached, not
    # silently discarded — it's exactly the kind of real risk Row 10's
    # preview-before-publish gate exists to catch before it goes out.
    qa_flagged: bool = False
    qa_flag_reason: Optional[str] = None

    # ── Understanding of the recording itself (video/audio) ───────────────
    # Filled by POST /media/{id}/analyze. Until then the asset is just a file
    # nothing in the app can "read"; after it, the words and their timings
    # feed real chapters, captions, and YouTube metadata instead of a guess
    # from the post text. All empty/None for images and never-analysed media.
    analysis_status: MediaAnalysisStatus = MediaAnalysisStatus.NONE
    analysis_error: Optional[str] = None
    transcript: list[MediaTranscriptWord] = []
    transcript_language: Optional[str] = None
    chapters: list[MediaChapter] = []
    # Still frame for players that shouldn't load the whole video (video only).
    poster_url: Optional[str] = None
