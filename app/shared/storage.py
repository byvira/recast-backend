"""Shared file storage helpers using Cloudinary."""

import cloudinary
import cloudinary.uploader
from enum import Enum

from app.core.config import settings

class ContentType(str, Enum):
    IMAGE = "recast_images"
    VIDEO = "recast_video"
    AUDIO = "recast_audio"
    THUMBNAIL = "recast_thumbnails"
    # PDF/ZIP carousel export artifacts — an arbitrary file, not an
    # image/video Cloudinary can transform, so it needs its own
    # resource_type branch below.
    EXPORT = "recast_exports"

_configured = False


def _ensure_configured() -> None:
    """Lazy, idempotent Cloudinary SDK init — nothing in the app ever
    called cloudinary.config() before, so every upload_file()/get_file_url()/
    delete_file() call would have failed at runtime despite CLOUDINARY_*
    being set in config/.env (this module was entirely unused until now).
    Same lazy-singleton pattern as token_store.py's _get_fernet()."""
    global _configured
    if _configured:
        return
    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True,
    )
    _configured = True


def _get_resource_type(content_type: ContentType) -> str:
    if content_type == ContentType.VIDEO or content_type == ContentType.AUDIO:
        return "video"  # Cloudinary uses "video" for audio too
    if content_type == ContentType.EXPORT:
        return "raw"  # arbitrary file (PDF/ZIP), not an image/video transform target
    return "image"

async def upload_file_detailed(
    file: bytes,
    content_type: ContentType,
    user_id: str,
    filename: str = None
) -> dict:
    """Upload file bytes to Cloudinary and return everything useful from its
    response, not just the URL: Cloudinary already measures width/height
    (images/video) and duration (video/audio) during upload, and callers used
    to throw all of it away — leaving MediaAsset's width/height/duration_s
    permanently unset for every upload.

    Returns {"url", "width", "height", "duration_s", "bytes", "format"};
    a key is None when Cloudinary didn't report it for that file type.
    """
    _ensure_configured()
    result = cloudinary.uploader.upload(
        file,
        upload_preset=content_type.value,       # uses the preset we created
        folder=f"recast/{content_type.value.replace('recast_', '')}/{user_id}",
        resource_type=_get_resource_type(content_type),
        public_id=filename,
        use_filename=bool(filename),
        unique_filename=True,
    )
    duration = result.get("duration")
    return {
        "url": result["secure_url"],
        "width": result.get("width"),
        "height": result.get("height"),
        "duration_s": float(duration) if duration is not None else None,
        "bytes": result.get("bytes"),
        "format": result.get("format"),
    }


async def upload_file(
    file: bytes,
    content_type: ContentType,
    user_id: str,
    filename: str = None
) -> str:
    """Upload file bytes to Cloudinary under the correct preset folder.

    Args:
        file: Raw bytes of the file to upload.
        content_type: Type of content (image, video, audio, thumbnail).
        user_id: ID of the user uploading the file.
        filename: Optional original filename.

    Returns:
        Secure Cloudinary URL pointing to the uploaded file.
    """
    return (await upload_file_detailed(file, content_type, user_id, filename))["url"]


async def get_file_url(public_id: str, content_type: ContentType) -> str:
    """Return a Cloudinary URL for the given public_id.

    Args:
        public_id: Cloudinary public ID of the file.
        content_type: Type of content to determine resource type.

    Returns:
        Accessible secure URL for the file.
    """
    _ensure_configured()
    resource_type = _get_resource_type(content_type)
    return cloudinary.utils.cloudinary_url(
        public_id,
        resource_type=resource_type,
        secure=True
    )[0]


async def delete_file(public_id: str, content_type: ContentType) -> bool:
    """Delete a file from Cloudinary.

    Args:
        public_id: Cloudinary public ID of the file.
        content_type: Type of content to determine resource type.

    Returns:
        True if deletion was successful.
    """
    _ensure_configured()
    result = cloudinary.uploader.destroy(
        public_id,
        resource_type=_get_resource_type(content_type)
    )
    return result.get("result") == "ok"


# ── Private files (support attachments) ──────────────────────────────────────
# Uploaded with delivery type "authenticated": the plain URL does not work, the
# only way to read the file is a signed link that expires. The signing uses the
# API secret, so no link can be built without this server.
PRIVATE_FOLDER = "recast/private/support"


def upload_private_file(file: bytes, folder_id: str, public_id: str) -> str:
    """Upload bytes as a private (authenticated) raw file. Returns its
    Cloudinary public_id, the only thing to store: never a URL. Blocking call;
    run it in a thread from async code."""
    _ensure_configured()
    result = cloudinary.uploader.upload(
        file,
        folder=f"{PRIVATE_FOLDER}/{folder_id}",
        resource_type="raw",
        type="authenticated",
        public_id=public_id,
        unique_filename=True,
        overwrite=False,
    )
    return result["public_id"]


def signed_private_url(public_id: str, expires_in_seconds: int = 300) -> str:
    """A time-limited download link for a private raw file. Pure signing, no
    network call."""
    import time

    _ensure_configured()
    return cloudinary.utils.private_download_url(
        public_id,
        "",
        resource_type="raw",
        type="authenticated",
        expires_at=int(time.time()) + expires_in_seconds,
    )


def delete_private_file(public_id: str) -> bool:
    """Blocking; run it in a thread from async code."""
    _ensure_configured()
    result = cloudinary.uploader.destroy(public_id, resource_type="raw", type="authenticated")
    return result.get("result") == "ok"

