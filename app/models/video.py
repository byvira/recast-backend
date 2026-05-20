"""Pydantic models for video pipeline inputs and outputs."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class VideoInput(BaseModel):
    """Input parameters for the video processing pipeline."""

    source_url: str
    target_platform: str = "youtube"
    style: str = "cinematic"
    duration_limit: int = 60
    captions: bool = True
    thumbnail_prompt: Optional[str] = None


class VideoOutput(BaseModel):
    """Result produced by the video processing pipeline."""

    output_url: str
    highlights: list[str]
    script: str
    duration_seconds: float
    created_at: datetime
