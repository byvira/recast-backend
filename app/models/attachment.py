"""PieceAttachment — one asset attached to a post (content_pieces.attachments).

The post is the only unit that gets published; images, audio and video are
attached to it. There is no bundle object: a post holds a plain list of these,
each naming the asset and the version of it that was attached, so a later change
to the asset can be spotted (stale) and a post's metrics can roll up to its
assets.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

AttachmentAssetType = Literal["image", "audio", "video", "upload"]


class PieceAttachment(BaseModel):
    id: str
    asset_type: AttachmentAssetType
    # image_assets.id for "image", audio_assets.id for "audio" and "video" (a
    # video is a clip rendered from a recording), None for a plain upload.
    asset_id: Optional[str] = None
    # The asset's version_count when it was attached. None for uploads and videos.
    asset_version: Optional[int] = None
    media_id: str
    slide_number: Optional[int] = None   # image only
    clip_id: Optional[str] = None        # video only
    # The post's version_count when it was attached, to spot text that changed after.
    piece_version: int = 1
    attached_at: datetime
    attached_by: str
    refreshed_at: Optional[datetime] = None
    # What the picture shows, for people who can't see it. Sent to platforms that take it (Bluesky today).
    alt_text: Optional[str] = Field(default=None, max_length=1000)


class AttachRequest(BaseModel):
    asset_type: AttachmentAssetType
    asset_id: Optional[str] = None
    slide_number: Optional[int] = None
    clip_id: Optional[str] = None
    # Only for asset_type "upload": a file already in the media library.
    media_id: Optional[str] = None
    alt_text: Optional[str] = Field(default=None, max_length=1000)


class ReorderAttachmentsRequest(BaseModel):
    # Every attachment on the post, in the order they should go out. The first one is the primary.
    attachment_ids: list[str] = Field(min_length=1, max_length=50)


class UpdateAttachmentRequest(BaseModel):
    # None or empty clears it.
    alt_text: Optional[str] = Field(default=None, max_length=1000)


class RefreshAttachmentsRequest(BaseModel):
    # Only refresh attachments of this asset; empty means every attachment.
    asset_id: Optional[str] = None
