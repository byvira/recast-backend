"""Pydantic models for audio pipeline inputs and outputs."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class AudioInput(BaseModel):
    """Input parameters for the audio generation pipeline."""

    script: str
    voice_id: str = "default"
    language: str = "en"
    speed: float = 1.0
    background_music: Optional[str] = None


class AudioOutput(BaseModel):
    """Result produced by the audio generation pipeline."""

    file_url: str
    duration_seconds: float
    transcript: str
    voice_id: str
    created_at: datetime
