"""Pydantic models for user accounts."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr


class User(BaseModel):
    """Authenticated user record returned from the API."""

    id: str
    email: EmailStr
    name: str
    tier: str = "free"
    is_active: bool = True
    created_at: datetime


class UserCreate(BaseModel):
    """Payload required to register a new user."""

    email: EmailStr
    name: str
    password: str
    tier: Optional[str] = "free"
