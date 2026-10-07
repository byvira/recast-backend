"""Request and response shapes for the public waitlist."""

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

WaitlistRole = Literal["creator", "team", "agency", "other"]
WaitlistTeamSize = Literal["solo", "2-5", "6-20", "21+"]

# Where people say they post. Anything else is ignored, so the list a lead sends can never hold free text.
PLATFORM_CHOICES = frozenset({"linkedin", "instagram", "facebook", "threads", "bluesky", "youtube", "x", "other"})

_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class WaitlistUtm(BaseModel):
    source: Optional[str] = Field(default=None, max_length=80)
    medium: Optional[str] = Field(default=None, max_length=80)
    campaign: Optional[str] = Field(default=None, max_length=80)


class WaitlistJoinBody(BaseModel):
    email: str = Field(..., max_length=254)
    source: str = Field(default="unknown", max_length=60)
    referral_code: Optional[str] = Field(default=None, max_length=40)
    utm: Optional[WaitlistUtm] = None
    turnstile_token: str = ""

    @field_validator("email")
    @classmethod
    def _clean_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL.match(value):
            raise ValueError("Please enter a valid email address.")
        return value


class WaitlistJoinResponse(BaseModel):
    status: Literal["joined", "already_joined"]
    # Empty when the address was already on the list: a share code is only ever shown to the person who just joined with it.
    referral_code: str = ""


class WaitlistProfileBody(BaseModel):
    role: Optional[WaitlistRole] = None
    team_size: Optional[WaitlistTeamSize] = None
    platforms: Optional[list[str]] = Field(default=None, max_length=8)

    @field_validator("platforms")
    @classmethod
    def _known_platforms(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        return sorted({item for item in value if item in PLATFORM_CHOICES})
