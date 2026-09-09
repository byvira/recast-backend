"""Pydantic models for user accounts, OTP flows, and auth responses."""

from datetime import datetime
from enum import Enum
from typing import Optional
from typing import Any
from pydantic import BaseModel, Field


class SocialAccount(BaseModel):
    """
    Connected social media account for a user.
    Tokens are stored encrypted in MongoDB — never exposed in API responses.
    """
    platform: str                           # linkedin, instagram, threads, facebook, reddit, bluesky
    username: str = ""                      # display name on the platform
    platform_user_id: str = ""             # platform's own ID for this user
    is_active: bool = True
    connected_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    last_refreshed_at: Optional[datetime] = None
    # NOTE: access_token and refresh_token are NOT in this model
    # They live only in MongoDB encrypted — never in Pydantic responses


class SocialAccountResponse(BaseModel):
    """
    Safe public-facing social account — no tokens ever.
    Used in API responses when listing connected accounts.
    """
    platform: str
    username: str
    platform_user_id: str
    is_active: bool
    connected_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

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
    social_accounts: list[SocialAccount] = []    
    brand_profiles: list[str] = []
    created_at: datetime
    last_active: datetime


class UserProfileResponse(BaseModel):
    id:                  str
    name:                str
    username:            str
    email: str | None = None  
    phone: str | None = None  
    avatar_url:          str        = ""
    bio:                 str        = ""
    website:             str        = ""
    timezone:            str        = "UTC"
    language:            str        = "en"
    preferred_platforms: list[str]  = []
    plan:                str        = "free"
    credits_used:        int        = 0
    credits_limit:       int        = 100
    onboarding_done:     bool       = False
    brand_profiles:      list[str]  = []
    social_accounts:     list[Any]  = []       
    auth_identifiers:    list[str]  = []    
    last_active:         datetime | None = None # 
    created_at:          datetime


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
