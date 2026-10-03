"""
Small helpers that make a picture acceptable to a platform before it is sent, so a post does not lose its picture
because of its file type or size.

Both rules below come from the platforms' public documentation as I know it (Instagram photo posts take JPEG; a Bluesky
image has to be under about 1 MB). They are NOT confirmed against a live account yet, and the code is written so that when
they do not apply (a URL that is not on our image host, a picture that is already small) nothing is changed.
"""

import io
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# A little under the 1,000,000 byte limit Bluesky documents for an image, so rounding in the upload never tips it over.
BLUESKY_MAX_IMAGE_BYTES = 950_000


def jpeg_url(url: str) -> str:
    """The same picture delivered as a JPEG, for platforms that only take JPEG (Instagram photo posts). Only a picture on
    our own image host (Cloudinary) can be converted this way; any other URL is returned unchanged. A URL already asking
    for a format is left alone."""
    marker = "/image/upload/"
    if "res.cloudinary.com" not in url or marker not in url:
        return url
    head, _, tail = url.partition(marker)
    first = tail.split("/", 1)[0]
    if "f_" in first:  # a transformation that already names a format
        return url
    return f"{head}{marker}f_jpg,q_90/{tail}"


def fit_image_for_bluesky(data: bytes, mime_type: Optional[str]) -> tuple[bytes, str]:
    """(bytes, mime type) of the picture, shrunk to fit Bluesky's size limit when it is over it. A picture already under the
    limit is returned exactly as it was. When it cannot be read or cannot be made small enough the original is returned and
    the platform decides, as it did before."""
    mime = mime_type or "image/png"
    if len(data) <= BLUESKY_MAX_IMAGE_BYTES:
        return data, mime
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(data))
        image = image.convert("RGB")  # JPEG has no transparency
        scale = 1.0
        for _ in range(6):
            candidate = image if scale == 1.0 else image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
            for quality in (88, 78, 68, 58):
                buf = io.BytesIO()
                candidate.save(buf, format="JPEG", quality=quality, optimize=True)
                if buf.tell() <= BLUESKY_MAX_IMAGE_BYTES:
                    return buf.getvalue(), "image/jpeg"
            scale *= 0.85
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not shrink the picture for Bluesky: %s", exc)
    return data, mime
