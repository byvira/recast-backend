"""Pydantic models for quick action chip endpoints."""

from typing import Optional
from pydantic import BaseModel


class ApplyChipRequest(BaseModel):
    content: str
    chip: str
    platform: str
    brand_id: str
    piece_id: Optional[str] = None


class ApplyChipResponse(BaseModel):
    original: str
    refined: str
    chip: str
    platform: str
    word_count: int
    char_count: int
    changed: bool
    error: Optional[str] = None


class GetChipsResponse(BaseModel):
    platform: str
    chips: list[str]
    total: int