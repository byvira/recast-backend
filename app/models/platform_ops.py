"""Request bodies for the Ops platform routes (stage, rollout, live test, facts, test send)."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class StageChange(BaseModel):
    to: Literal["not_started", "in_setup", "live", "paused", "retired"]
    reason: str = Field("", max_length=500)
    # Shown to members when a platform is paused. Left empty, a plain default is used.
    member_message: str = Field("", max_length=500)
    # Retiring asks for the platform's name to be typed back.
    confirm_name: str = Field("", max_length=100)
    version: int = Field(..., ge=0)


class RolloutChange(BaseModel):
    scope: Literal["ops_only", "selected", "everyone"]
    workspace_ids: list[str] = Field(default_factory=list, max_length=200)
    version: int = Field(..., ge=0)


class LiveTestRecord(BaseModel):
    note: str = Field("", max_length=500)
    version: int = Field(..., ge=0)


class FactsVerified(BaseModel):
    source_url: str = Field(..., min_length=8, max_length=500)
    version: int = Field(..., ge=0)


class TestSend(BaseModel):
    text: Optional[str] = Field(None, max_length=500)


class PurgeCredentials(BaseModel):
    confirm_name: str = Field(..., max_length=100)
    reason: str = Field("", max_length=500)
