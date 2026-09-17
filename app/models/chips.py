"""Pydantic models for quick action chip endpoints."""

from typing import Optional
from pydantic import BaseModel, Field


class ApplyChipRequest(BaseModel):
    content: str
    chip: str
    platform: str
    brand_id: str
    piece_id: Optional[str] = None
    # Feature 5 — user-authored one-click refinements, not limited to the
    # fixed CHIP_PROMPTS set. When set, this is the actual instruction sent
    # to the LLM and `chip` is just its display label/version-history tag
    # (any non-empty string — not validated against CHIP_PROMPTS).
    custom_instruction: Optional[str] = Field(default=None, max_length=300)


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