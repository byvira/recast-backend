"""Pydantic models for user accounts, OTP flows, and auth responses."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class UserPlan(str, Enum):
    """Subscription tier for a user account."""

    FREE = "free"
    PRO = "pro"
    SCALE = "scale"


class OTPChannel(str, Enum):
    """Delivery channel for one-time passwords."""

    EMAIL = "email"
    SMS = "sms"


class OTPRequestBody(BaseModel):
    """Request body for sending an OTP."""

    identifier: str = Field(..., description="Email or E.164 phone number")
    channel: OTPChannel


class OTPVerifyBody(BaseModel):
    """Request body for verifying an OTP code."""

    identifier: str
    otp: str = Field(..., min_length=6, max_length=6)
    channel: OTPChannel


class LoginBody(BaseModel):
    """Request body for login — OTP already verified at /verify-otp step."""

    identifier: str
    channel: OTPChannel


class SignupCompleteBody(BaseModel):
    """Request body to complete registration after OTP verification."""

    identifier: str
    channel: OTPChannel
    name: str = Field(..., min_length=1, max_length=100)
    username: str = Field(
        ...,
        min_length=3,
        max_length=30,
        pattern=r"^[a-zA-Z0-9_]+$",
    )


class UserUpdateBody(BaseModel):
    """Fields a user may update on their own profile."""

    name: Optional[str] = Field(None, min_length=1, max_length=100)
    username: Optional[str] = Field(
        None,
        min_length=3,
        max_length=30,
        pattern=r"^[a-zA-Z0-9_]+$",
    )
    bio: Optional[str] = Field(None, max_length=500)
    website: Optional[str] = None
    avatar_url: Optional[str] = None
    timezone: Optional[str] = None
    language: Optional[str] = None
    preferred_platforms: Optional[list[str]] = None


class UserProfile(BaseModel):
    """Full internal user document (never exposed directly via API)."""

    id: str
    email: Optional[str] = None
    phone: Optional[str] = None
    auth_identifiers: list[str] = []
    name: str
    username: str
    avatar_url: str = ""
    bio: str = ""
    website: str = ""
    timezone: str = "UTC"
    language: str = "en"
    preferred_platforms: list[str] = []
    plan: UserPlan = UserPlan.FREE
    credits_used: int = 0
    credits_limit: int = 100
    onboarding_done: bool = False
    brand_profiles: list[str] = []
    created_at: datetime
    last_active: datetime


class UserProfileResponse(BaseModel):
    """Safe public-facing profile — strips email, phone, and credits internals."""

    id: str
    name: str
    username: str
    avatar_url: str
    bio: str
    website: str
    timezone: str
    language: str
    preferred_platforms: list[str]
    plan: UserPlan
    credits_used: int
    credits_limit: int
    onboarding_done: bool
    brand_profiles: list[str]
    created_at: datetime


class PublicProfileResponse(BaseModel):
    """Fully public profile — no private fields whatsoever."""

    username: str
    name: str
    avatar_url: str
    bio: str
    website: str


class AuthResponse(BaseModel):
    """Successful auth response containing token pair and user profile."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserProfileResponse


class OTPSentResponse(BaseModel):
    """Response confirming OTP was dispatched."""

    message: str
    cooldown_seconds: int


class VerifyOTPResponse(BaseModel):
    """Result of OTP verification — tells client whether to go to login or signup."""

    valid: bool
    is_new_user: bool
