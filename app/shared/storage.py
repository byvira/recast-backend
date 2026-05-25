"""Shared file storage helpers using Cloudinary."""

import cloudinary.uploader
from enum import Enum

class ContentType(str, Enum):
    IMAGE = "recast_images"
    VIDEO = "recast_video"
    AUDIO = "recast_audio"
    THUMBNAIL = "recast_thumbnails"

def _get_resource_type(content_type: ContentType) -> str:
    if content_type == ContentType.VIDEO or content_type == ContentType.AUDIO:
        return "video"  # Cloudinary uses "video" for audio too
    return "image"

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
    result = cloudinary.uploader.upload(
        file,
        upload_preset=content_type.value,       # uses the preset we created
        folder=f"recast/{content_type.value.replace('recast_', '')}/{user_id}",
        resource_type=_get_resource_type(content_type),
        public_id=filename,
        use_filename=bool(filename),
        unique_filename=True,
    )
    return result["secure_url"]


async def get_file_url(public_id: str, content_type: ContentType) -> str:
    """Return a Cloudinary URL for the given public_id.

    Args:
        public_id: Cloudinary public ID of the file.
        content_type: Type of content to determine resource type.

    Returns:
        Accessible secure URL for the file.
    """
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
    result = cloudinary.uploader.destroy(
        public_id,
        resource_type=_get_resource_type(content_type)
    )
    return result.get("result") == "ok"