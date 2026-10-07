"""Request shape for the public contact form."""

import re

from pydantic import BaseModel, Field, field_validator

_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class ContactBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=254)
    message: str = Field(..., min_length=10, max_length=4000)
    # Filled in by people only when they are writing about a team larger than the plans cover.
    topic: str = Field(default="general", max_length=40)
    turnstile_token: str = ""

    @field_validator("name", "message")
    @classmethod
    def _trim(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be empty.")
        return value

    @field_validator("email")
    @classmethod
    def _clean_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL.match(value):
            raise ValueError("Please enter a valid email address.")
        return value
