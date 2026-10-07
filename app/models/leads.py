"""Statuses and request shapes for the staff view of waitlist leads and contact messages."""

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

WaitlistStatus = Literal["joined", "reviewed", "hold", "invited", "activated", "expired", "unsubscribed"]
ContactStatus = Literal["new", "open", "answered", "closed", "spam"]

# What staff can set by hand. The other waitlist statuses are set by the invite and sign-up flow, never typed in.
STAFF_WAITLIST_STATUSES = frozenset({"joined", "reviewed", "hold"})
# "answered" is set by sending a reply, not by choosing it.
STAFF_CONTACT_STATUSES = frozenset({"new", "open", "closed", "spam"})

# Topics a visitor can pick on the contact form. Anything else is stored as "general".
CONTACT_TOPICS = frozenset({"general", "seats", "feedback", "press"})
SALES_TOPIC = "seats"


class LeadNote(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)

    @field_validator("text")
    @classmethod
    def _trim(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A note cannot be empty.")
        return value


class WaitlistUpdate(BaseModel):
    status: Optional[Literal["joined", "reviewed", "hold"]] = None
    note: Optional[str] = Field(default=None, max_length=1000)


class ContactUpdate(BaseModel):
    status: Optional[Literal["new", "open", "closed", "spam"]] = None
    # True takes the message for the signed-in staff member, False hands it back.
    assign_to_me: Optional[bool] = None
    note: Optional[str] = Field(default=None, max_length=1000)


class StaffEmail(BaseModel):
    """An email a staff member writes to a lead or to someone who wrote in."""

    subject: str = Field(..., min_length=2, max_length=150)
    message: str = Field(..., min_length=2, max_length=5000)

    @field_validator("subject", "message")
    @classmethod
    def _trim(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("This field cannot be empty.")
        return value

    @field_validator("subject")
    @classmethod
    def _one_line(cls, value: str) -> str:
        # A line break in a subject could add headers to the email, so it is refused.
        if any(ch in value for ch in (chr(13), chr(10))):
            raise ValueError("The subject must be a single line.")
        return value


class ContactReply(BaseModel):
    message: str = Field(..., min_length=2, max_length=4000)

    @field_validator("message")
    @classmethod
    def _trim(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("A reply cannot be empty.")
        return value
