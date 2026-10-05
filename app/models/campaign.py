"""Pydantic models for campaign management.

A campaign groups multiple generation runs (one topic cluster -> N days of
content via run_batch_pipeline()) under one tracked entity, with real
aggregate progress across its pieces, optional per-day platform variation
(`platforms_by_day`), and optional recurring auto-generation
(`cadence.frequency` + `cadence.next_run_at`, polled by
app.workers.campaign_scheduler).

Every post is a text post. A campaign can also make pictures and narration for each post
(`media_plan`, off by default); `content_types` is derived from that plan. Video is accepted in
the plan but not generated yet.
"""

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class ContentType(str, Enum):
    TEXT = "text"
    AUDIO = "audio"
    VIDEO = "video"
    IMAGE = "image"


class CampaignSourceType(str, Enum):
    """Matches the Pipeline page's original Source Intake tabs. Only
    RAW_TEXT and ARTICLE_URL have anything real behind them —
    run_batch_pipeline() takes a plain topic string, and article_url is
    scraped into one via the same scrape_url() /text/repurpose already
    uses. YOUTUBE/AUDIO_UPLOAD/PODCAST_RSS need real transcription/video
    pipelines that don't exist anywhere in this backend yet (app/api/v1/
    audio.py and video.py are unimplemented health-check stubs) — create_
    campaign rejects those with a clear 400 rather than silently accepting
    a source it can't actually process."""

    RAW_TEXT = "raw_text"
    ARTICLE_URL = "article_url"
    YOUTUBE = "youtube"
    AUDIO_UPLOAD = "audio_upload"
    PODCAST_RSS = "podcast_rss"


# A recording (AUDIO_UPLOAD) is a source: its topic_cluster is the id of a recording in the workspace. A YouTube or podcast link
# is imported into a recording in the Audio pipeline first, so those two are asked to do that rather than accepted as a link.
UNSUPPORTED_CAMPAIGN_SOURCES = {
    CampaignSourceType.YOUTUBE,
    CampaignSourceType.PODCAST_RSS,
}


class CampaignStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"


class CampaignCadence(BaseModel):
    """How generate-next-batch is run. `next_run_at` is server-computed only
    (set/advanced by generate_campaign_batch, never trusted from a client
    body even though this model is embedded directly in Create/Update
    requests) — it's what app.workers.campaign_scheduler polls to find
    campaigns due for automatic regeneration."""

    frequency: str = "manual"  # "manual" | "daily" | "weekly"
    days_per_batch: int = 7  # passed straight to run_batch_pipeline's `days`
    next_run_at: Optional[datetime] = None


class CampaignAudioOptions(BaseModel):
    """Narration settings for every post of a campaign. A post is voiced as written; if a maximum
    length is set and the post would run longer, it is cut at a sentence end to fit."""

    max_seconds: Optional[int] = Field(None, ge=15, le=600)
    words_per_minute: Optional[int] = Field(None, ge=100, le=200)


class CampaignImageOptions(BaseModel):
    """Image settings for every post. `layout` is one of the image pipeline's sizes."""

    layout: str = "quote_1_1"
    # What is written on the picture: a short headline (default) or nothing at all.
    text: Literal["headline", "none"] = "headline"
    # The brand's logo on every picture (default on; only does anything when the brand has a logo).
    logo: bool = True
    # The brand's mascot in a corner of every picture (off unless asked; only does anything when the brand has one).
    mascot: bool = False

    @model_validator(mode="after")
    def _known_layout(self) -> "CampaignImageOptions":
        from app.models.image_asset import LayoutPreset

        if self.layout not in {l.value for l in LayoutPreset}:
            raise ValueError(f"Unknown image size: {self.layout}")
        return self


class CampaignVideoOptions(BaseModel):
    """Video settings. Saved now so video can be switched on later; nothing is made from them yet."""

    duration_seconds: int = Field(30, ge=5, le=180)
    aspect: str = "9:16"

    @model_validator(mode="after")
    def _known_aspect(self) -> "CampaignVideoOptions":
        if self.aspect not in ("9:16", "16:9", "1:1"):
            raise ValueError("Video shape must be 9:16, 16:9 or 1:1")
        return self


class CampaignMediaPlan(BaseModel):
    """Whether each post of a campaign also gets media, and what kind. Off by default: a campaign
    makes text only, and nothing is generated (or paid for) until the member asks for it.
    `kinds` are any of "image", "audio", "video" (video is accepted and stored so it can be
    switched on later, but is not generated yet). `count_per_post` is how many of EACH chosen
    kind are made for every post."""

    enabled: bool = False
    kinds: list[str] = Field(default_factory=list)
    count_per_post: int = Field(1, ge=1, le=5)
    audio: CampaignAudioOptions = Field(default_factory=CampaignAudioOptions)
    image: CampaignImageOptions = Field(default_factory=CampaignImageOptions)
    video: CampaignVideoOptions = Field(default_factory=CampaignVideoOptions)

    @model_validator(mode="after")
    def _normalise(self) -> "CampaignMediaPlan":
        seen: list[str] = []
        for kind in self.kinds:
            kind = str(kind).lower()
            if kind not in ("image", "audio", "video"):
                raise ValueError(f"Unknown media kind: {kind}")
            if kind not in seen:
                seen.append(kind)
        self.kinds = seen
        if not seen:
            self.enabled = False
        return self


class Campaign(BaseModel):
    """A content campaign grouping multiple generation runs under one
    tracked entity, with real aggregate progress across its pieces."""

    id: str
    workspace_id: str
    brand_id: str
    name: str
    topic_cluster: str
    source_type: CampaignSourceType = CampaignSourceType.RAW_TEXT
    source_url: Optional[str] = None
    # For a recording source: which recording, its title and length. The text itself is in topic_cluster.
    source_ref: Optional[dict] = None
    content_types: list[ContentType] = Field(default_factory=lambda: [ContentType.TEXT])
    platforms: list[str] = Field(default_factory=list)  # real Platform values, e.g. "LinkedIn"
    # Optional per-day platform override — platforms_by_day[i] is used for
    # day i instead of the flat `platforms` list, when present. None (the
    # default) reproduces the flat-list behaviour every existing campaign
    # already relies on.
    platforms_by_day: Optional[list[list[str]]] = None
    cadence: CampaignCadence = Field(default_factory=CampaignCadence)
    media_plan: CampaignMediaPlan = Field(default_factory=CampaignMediaPlan)
    # The language every post of this campaign is written in, for example "ta+en". None means follow the workspace.
    language: Optional[str] = None
    status: CampaignStatus = CampaignStatus.DRAFT
    # Cloudinary secure_url, set via POST /{campaign_id}/thumbnail. User-
    # uploaded only — there is no real image-generation pipeline to derive
    # one from (see ContentType.IMAGE's stub status).
    thumbnail_url: Optional[str] = None
    # Every piece_id generated across every generate-next-batch run for
    # this campaign — content_pieces also carries campaign_id directly
    # (mirrors how session_id already links pieces), so this list is a
    # convenience/ordering record, not the source of truth for membership.
    piece_ids: list[str] = Field(default_factory=list)
    last_generated_at: Optional[datetime] = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class CreateCampaignRequest(BaseModel):
    name: str
    brand_id: str
    # For RAW_TEXT this is the topic/brief itself. For ARTICLE_URL it's the
    # URL to scrape — the route replaces it with the scraped article text
    # before saving, same as /text/repurpose's source_type="url" handling.
    topic_cluster: str
    source_type: CampaignSourceType = CampaignSourceType.RAW_TEXT
    platforms: list[str]
    platforms_by_day: Optional[list[list[str]]] = None
    cadence: CampaignCadence = Field(default_factory=CampaignCadence)
    media_plan: CampaignMediaPlan = Field(default_factory=CampaignMediaPlan)
    language: Optional[str] = Field(default=None, max_length=16)


class UpdateCampaignRequest(BaseModel):
    name: Optional[str] = None
    topic_cluster: Optional[str] = None
    platforms: Optional[list[str]] = None
    platforms_by_day: Optional[list[list[str]]] = None
    status: Optional[CampaignStatus] = None
    cadence: Optional[CampaignCadence] = None
    media_plan: Optional[CampaignMediaPlan] = None
    language: Optional[str] = Field(default=None, max_length=16)
