"""Pydantic models for refinement chat endpoint."""

from typing import Optional
from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str     # "user" or "assistant"
    content: str


class RefineChatRequest(BaseModel):
    """
    Request body for POST /api/v1/text/refine-chat.

    messages:   full conversation history including the new user message
                first message should contain the original content
                subsequent messages are the refinement conversation

    brand_id:   used to load brand context and banned words
    platform:   target platform — affects formatting rules
    piece_id:   optional — if provided saves a new version after refinement
    """
    messages: list[ChatMessage] = Field(..., min_length=1)
    brand_id: str
    platform: str
    piece_id: Optional[str] = None


class RefineChatResponse(BaseModel):
    refined: str
    platform: str
    word_count: int
    char_count: int
    turn: int          
    piece_id: Optional[str] = None
    version_saved: bool = False