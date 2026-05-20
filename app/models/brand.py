"""Pydantic models for brand profile and voice configuration."""

from typing import Optional

from pydantic import BaseModel


class BrandProfile(BaseModel):
    """Core brand identity settings used across all pipelines."""

    name: str
    industry: str
    tone: str = "professional"
    colors: list[str] = []
    fonts: list[str] = []
    tagline: Optional[str] = None
    website: Optional[str] = None


class BrandVoiceConfig(BaseModel):
    """Detailed voice and style rules for content generation."""

    vocabulary: list[str] = []
    forbidden_words: list[str] = []
    style_guide: dict = {}
    examples: list[str] = []
    preferred_cta: Optional[str] = None
