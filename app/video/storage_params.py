"""Signed upload parameters for the render worker.

The worker uploads its finished video straight to storage, so the video never passes through the backend (which has 512 MB
of memory). It is given parameters that are signed here with the storage secret, valid for minutes and scoped to one folder
and one name: the secret itself never leaves the backend.
"""
from __future__ import annotations

import re
import time

import cloudinary.utils

from app.core.config import settings
from app.shared.storage import ContentType, _ensure_configured

# Files bigger than this go up in pieces (each at least 5 MB except the last, which is what the storage service requires).
CHUNK_FROM_BYTES = 20 * 1024 * 1024
CHUNK_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 4 * 1024 * 1024 * 1024
MAX_FRAME_BYTES = 8 * 1024 * 1024


def _safe_stem(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return re.sub(r"[^A-Za-z0-9_-]", "_", stem)[:60] or "file"


def upload_params(*, workspace_id: str, job_id: str, purpose: str, filename: str, nbytes: int) -> dict:
    """What the worker needs to upload one file: a signed multipart form, and a piece size when the file is large."""
    if purpose not in ("video", "frame"):
        raise ValueError("purpose must be video or frame")
    limit = MAX_VIDEO_BYTES if purpose == "video" else MAX_FRAME_BYTES
    if nbytes <= 0 or nbytes > limit:
        raise ValueError(f"A {purpose} file must be between 1 byte and {limit // (1024 * 1024)} MB.")
    _ensure_configured()
    resource = "video" if purpose == "video" else "image"
    params: dict[str, str | int] = {
        "folder": f"recast/{'video' if purpose == 'video' else 'video_frames'}/{workspace_id}",
        "public_id": f"{job_id}_{purpose}_{_safe_stem(filename)}",
        "overwrite": "true",
        "timestamp": int(time.time()),
    }
    if purpose == "video":
        params["upload_preset"] = ContentType.VIDEO.value
    signature = cloudinary.utils.api_sign_request(params, settings.CLOUDINARY_API_SECRET)
    out: dict = {
        "method": "POST",
        "url": f"https://api.cloudinary.com/v1_1/{settings.CLOUDINARY_CLOUD_NAME}/{resource}/upload",
        "fields": {**{k: str(v) for k, v in params.items()}, "api_key": settings.CLOUDINARY_API_KEY, "signature": signature},
        "file_field": "file",
    }
    if nbytes > CHUNK_FROM_BYTES:
        out["chunk_bytes"] = CHUNK_BYTES
    return out


def source_upload_params(*, workspace_id: str, upload_id: str, filename: str, nbytes: int) -> dict:
    """Signed parameters for a member's own source video, uploaded from the browser in pieces. The folder and name are fixed
    here, so a browser can only write the one file it was given."""
    params = upload_params(workspace_id=workspace_id, job_id=upload_id, purpose="video", filename=filename, nbytes=nbytes)
    _ensure_configured()
    fields = {
        "folder": f"recast/video_source/{workspace_id}",
        "public_id": f"{upload_id}_{_safe_stem(filename)}",
        "overwrite": "true",
        "timestamp": int(time.time()),
        "upload_preset": ContentType.VIDEO.value,
    }
    signature = cloudinary.utils.api_sign_request(fields, settings.CLOUDINARY_API_SECRET)
    params["fields"] = {**{k: str(v) for k, v in fields.items()}, "api_key": settings.CLOUDINARY_API_KEY, "signature": signature}
    params["chunk_bytes"] = CHUNK_BYTES
    params["public_id"] = fields["public_id"]
    params["folder"] = fields["folder"]
    return params


def is_our_storage_url(url: str) -> bool:
    """True only for a file in this account's own storage, so a worker can never point the backend at another address."""
    return bool(url) and url.startswith(f"https://res.cloudinary.com/{settings.CLOUDINARY_CLOUD_NAME}/")
