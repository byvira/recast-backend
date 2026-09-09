"""
Pydantic request and response models for scorer endpoints.
"""

from typing import Optional
from pydantic import BaseModel


class ScoreHookRequest(BaseModel):
    content: str
    platform: str
    brand_id: str


class ScoreReadabilityRequest(BaseModel):
    content: str
    platform: str


class HookAlternative(BaseModel):
    text: str
    style: str
    score: int
    reason: str


class ScoreHookResponse(BaseModel):
    current_hook: str
    current_score: int
    current_reason: str
    current_weakness: Optional[str] = None
    alternatives: list[HookAlternative]
    recommended: int
    recommended_content: str
    platform: str


class ScoreReadabilityResponse(BaseModel):
    score: float
    threshold: int
    grade: str
    grade_label: str
    word_count: int
    sentence_count: int
    avg_sentence_len: float
    complex_word_count: int
    issues: list[str]
    passed: bool
    platform: str