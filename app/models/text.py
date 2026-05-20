"""Pydantic models for text pipeline inputs and outputs."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class TextInput(BaseModel):
    """Input parameters for the text generation pipeline."""

    topic: str
    platform: str
    tone: str = "professional"
    word_count: int = 500
    keywords: Optional[list[str]] = None
    language: str = "en"


class TextOutput(BaseModel):
    """Result produced by the text generation pipeline."""

    content: str
    hooks: list[str]
    quality_score: float
    platform: str
    word_count: int
    created_at: datetime
