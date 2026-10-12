"""Every memory and size limit that protects the server, in one place.

Each is read from an environment variable, so moving to a bigger instance is a settings change and a restart, with no code
edit. The defaults are sized for the 512 MB instance (the app alone holds about 300 MB of it). The reasoning for each value is
in the module that uses it.

    Variable                      Default   What it limits
    HEAVY_WORK_CONCURRENCY        1         heavy jobs (exports, audio, background removal, ZIPs) running at once
    AUDIO_WORK_BUDGET_MB          150       peak memory the audio cleanup and mixing may use (about 2 min of stereo)
    UPLOAD_HARD_LIMIT_MB          50        the largest upload, whatever a workspace's own limit says
    LARGE_UPLOAD_MB               10        uploads this big wait their turn, one at a time
    IMAGE_MAX_MEGAPIXELS          40        the largest picture accepted
    EXPORT_MAX_MEGAPIXELS         24        the largest picture an export may produce
    EXPORT_ZIP_TOTAL_MB           40        total media in one library or campaign ZIP
    EXPORT_ZIP_FILE_MB            25        one file in a ZIP
    FETCHED_IMAGE_MAX_MB          25        a picture fetched back from storage
    STORED_AUDIO_MAX_MB           120       a stored recording loaded back for processing
    DESCRIBE_IMAGE_MAX_MB         4         a picture sent to the vision service for its description
    PUBLISH_VIDEO_MAX_MB          60        a video fetched into memory to be sent to a platform
    BG_REMOVAL_FREE_MB            300       free memory needed before Remove Background runs
"""
from __future__ import annotations

import os

_MB = 1024 * 1024


def _number(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def _mb(name: str, default: float) -> int:
    return int(_number(name, default) * _MB)


HEAVY_WORK_CONCURRENCY = max(1, int(_number("HEAVY_WORK_CONCURRENCY", 1)))
AUDIO_WORK_BUDGET_BYTES = _mb("AUDIO_WORK_BUDGET_MB", 150)
UPLOAD_HARD_LIMIT_BYTES = _mb("UPLOAD_HARD_LIMIT_MB", 50)
LARGE_UPLOAD_BYTES = _mb("LARGE_UPLOAD_MB", 10)
IMAGE_MAX_PIXELS = int(_number("IMAGE_MAX_MEGAPIXELS", 40) * 1_000_000)
EXPORT_MAX_PIXELS = int(_number("EXPORT_MAX_MEGAPIXELS", 24) * 1_000_000)
EXPORT_ZIP_TOTAL_BYTES = _mb("EXPORT_ZIP_TOTAL_MB", 40)
EXPORT_ZIP_FILE_BYTES = _mb("EXPORT_ZIP_FILE_MB", 25)
FETCHED_IMAGE_MAX_BYTES = _mb("FETCHED_IMAGE_MAX_MB", 25)
STORED_AUDIO_MAX_BYTES = _mb("STORED_AUDIO_MAX_MB", 120)
DESCRIBE_IMAGE_MAX_BYTES = _mb("DESCRIBE_IMAGE_MAX_MB", 4)
PUBLISH_VIDEO_MAX_BYTES = _mb("PUBLISH_VIDEO_MAX_MB", 60)
BG_REMOVAL_FREE_MB = int(_number("BG_REMOVAL_FREE_MB", 300))
