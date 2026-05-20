"""Pydantic models for image pipeline inputs and outputs."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ImageInput(BaseModel):
    """Input parameters for the image generation pipeline."""

    prompt: str
    style: str = "photorealistic"
    dimensions: str = "1024x1024"
    platform: str = "instagram"
    negative_prompt: Optional[str] = None


class ImageOutput(BaseModel):
    """Result produced by the image generation pipeline."""

    image_url: str
    alt_text: str
    metadata: dict
    dimensions: str
    created_at: datetime
