"""ImageAsset — a real carousel/single-image project, replacing the dead
`app/api/v1/image.py` decoy path. Follows `ContentPiece`/`ContentPieceVersion`'s
exact shape (app.models.text) — see pow/audio_image_pipeline/01-image-pipeline-plan.md
for the full plan this implements.

`MediaAsset` (app.models.media) stays the per-slide rendered file; this model is
the carousel/slide-sequence/governance wrapper around one or more of those files.
"""

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class LayoutPreset(str, Enum):
    QUOTE_1_1 = "quote_1_1"
    CAROUSEL_4_5 = "carousel_4_5"
    STORY_9_16 = "story_9_16"
    HERO_16_9 = "hero_16_9"
    BENTO = "bento"
    INFOGRAPHIC = "infographic"
    CHART = "chart"
    CODE_SNIPPET = "code_snippet"
    # Added per pow/audio_image_pipeline/06-full-workflow-and-localization.md
    # step 6 — Apple Podcasts' and Spotify's real minimum cover-art size.
    PODCAST_COVER = "podcast_cover"
    # Platform-sized canvases (same card style, the platform's own recommended pixel size).
    INSTAGRAM_SQUARE = "instagram_square"      # 1080x1080
    LINKEDIN_POST = "linkedin_post"            # 1200x627
    X_POST = "x_post"                          # 1600x900
    SOCIAL_SHARE = "social_share"              # 1200x630, Facebook link share and Open Graph
    YOUTUBE_THUMBNAIL = "youtube_thumbnail"    # 1280x720


class ImageApprovalStatus(str, Enum):  # mirrors ApprovalStatus in app.models.text
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ImageSourceType(str, Enum):
    """Not in the original plan's model — added 2026-09-26 mirroring
    AudioSourceType, per the user's real requirement that both pipelines
    support a manual/no-AI path (source_piece_id-driven repurposing is
    also AI-driven, so it's not its own source type here — the plan
    already covers that via source_piece_id, only the "no AI at all"
    case was missing)."""

    AI_GENERATED = "ai_generated"
    UPLOADED = "uploaded"


class Layer(BaseModel):
    """One editable element of a picture. Positions are fractions of the canvas, so a design works at any size. Every field
    is range checked here, so a bad value from the browser can never reach the drawing code."""

    id: str = Field(min_length=1, max_length=40)
    type: Literal["text", "shape", "image", "logo", "mascot", "icon"]
    name: str = Field("", max_length=60)
    x: float = Field(0.0, ge=-1.0, le=2.0)
    y: float = Field(0.0, ge=-1.0, le=2.0)
    w: float = Field(0.3, ge=0.001, le=3.0)
    h: float = Field(0.1, ge=0.001, le=3.0)
    rotation: float = Field(0.0, ge=-360.0, le=360.0)
    opacity: float = Field(1.0, ge=0.0, le=1.0)
    locked: bool = False
    hidden: bool = False
    # text (the box height of a text layer is not stored: it grows with the text)
    text: str = Field("", max_length=2000)
    font: Optional[str] = Field(None, max_length=60)  # a bundled family name; anything else falls back to Inter
    size: float = Field(0.05, ge=0.005, le=0.5)        # fraction of the canvas width
    bold: bool = True
    color: str = Field("#FFFFFF", pattern=r"^#[0-9a-fA-F]{6}$")
    align: Literal["left", "center", "right"] = "left"
    line_height: float = Field(1.25, ge=0.8, le=3.0)
    letter_spacing: float = Field(0.0, ge=-0.2, le=1.0)  # in em
    uppercase: bool = False
    shadow: bool = False
    box_color: Optional[str] = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    accent_word: str = Field("", max_length=80)
    accent_color: Optional[str] = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    # shape
    shape: Literal["rect", "ellipse", "gradient_up", "gradient_down"] = "rect"
    fill: str = Field("#000000", pattern=r"^#[0-9a-fA-F]{6}$")
    stroke: Optional[str] = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    stroke_w: float = Field(0.0, ge=0.0, le=0.2)
    radius: float = Field(0.0, ge=0.0, le=0.5)          # of the shape's shorter side
    # picture layers: logo and mascot always come from the brand; an uploaded image names a file in this workspace's library
    media_id: Optional[str] = Field(None, max_length=80)
    fit: Literal["contain", "cover"] = "contain"
    # icon
    icon: Optional[str] = Field(None, max_length=60)


class Slide(BaseModel):
    slide_number: int
    title: str
    slide_type: str
    # Optional — added 2026-09-26. LayoutPreset describes a *composited*
    # result (image_render.py's real output); a manually uploaded image
    # was never composited, so forcing a layout onto it would be a
    # fabricated field, not a real one.
    layout: Optional[LayoutPreset] = None
    media_id: Optional[str] = None  # -> MediaAsset.id, the rendered PNG for this slide
    text_content: dict = {}
    effects: dict = {}
    # The editable design: the clean picture (no words on it) plus the layers drawn over it. media_id above stays the
    # finished picture, drawn from these. Slides made before the editor existed have neither and stay as they were.
    background_media_id: Optional[str] = None
    layers: list[Layer] = []


class CommentPin(BaseModel):
    id: str
    slide_number: int
    author_id: str
    author_name: str
    x: float  # 0-100, matches the frontend's percent-based x/y
    y: float
    text: str
    created_at: datetime
    resolved: bool = False


class CommentPinCreate(BaseModel):
    slide_number: int
    x: float
    y: float
    text: str


class CommentPinUpdate(BaseModel):
    resolved: bool


class ImageAsset(BaseModel):
    """One ImageAsset = one carousel/single-image project."""

    id: str
    workspace_id: str
    # Not in the original plan's model sketch — added 2026-09-26 after
    # checking the real codebase: every other generation surface
    # (app.models.text's brand_id fields, app.api.v1.text's
    # _get_verified_brand) requires an explicit brand_id because a
    # workspace can hold several brand profiles with no "default" concept.
    # ImageAsset needs the same, for the same reason (which VisualIdentity
    # to snapshot brand_tokens from).
    brand_id: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    title: str
    # source_type/prompt/visual_profile — prompt and visual_profile made
    # Optional 2026-09-26 (were required): a manually uploaded image has
    # neither a generation prompt nor a chosen visual profile — both are
    # generation-time concepts that don't apply. source_type defaults to
    # AI_GENERATED so the existing Stage 2 generate endpoint's behavior
    # (and every already-created real ImageAsset) is unaffected.
    source_type: ImageSourceType = ImageSourceType.AI_GENERATED
    prompt: Optional[str] = None
    negative_prompt: Optional[str] = None
    seed: Optional[int] = None
    visual_profile: Optional[str] = None
    brand_tokens: dict = {}
    slides: list[Slide] = []
    comments: list[CommentPin] = []
    approval_status: ImageApprovalStatus = ImageApprovalStatus.PENDING
    approved_master_media_id: Optional[str] = None
    version_count: int = 1
    source_piece_id: Optional[str] = None  # repurpose-flow linkage + stale-upstream tracking
    source_content_hash: Optional[str] = None
    og_title: Optional[str] = None
    og_description: Optional[str] = None
    alt_text: Optional[str] = None
    file_naming_template: Optional[str] = None
    qa_flagged: bool = False
    qa_flag_reason: Optional[str] = None
    # The posts this picture is attached to: [{piece_id, attached_at}]. Written when it is attached to a post, so the
    # picture knows where it went and a post's results can be traced back to it.
    linked_pieces: list[dict] = []
    # The campaign this picture was made for, when it was made by a campaign run.
    campaign_id: Optional[str] = None


class ImageAssetVersion(BaseModel):  # mirrors ContentPieceVersion (app.models.text)
    version_id: str
    image_asset_id: str
    workspace_id: str
    user_id: str
    version_number: int
    slides_snapshot: list[Slide]
    action: str
    created_at: datetime


class ImageShareLink(BaseModel):  # mirrors invites.py's token/expiry pattern
    token: str
    image_asset_id: str
    workspace_id: str
    created_by: str
    created_at: datetime
    expires_at: datetime
    revoked: bool = False
