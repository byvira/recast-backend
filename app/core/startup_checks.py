"""Checks made once at startup, so a misconfigured deploy is stopped or shouted about at once instead of failing later in use."""
from __future__ import annotations


def fatal_problems(*, environment: str, hosted: bool) -> list[str]:
    """Problems that must stop the app from starting.

    A hosted deploy that is not marked production would print sign-in codes to the log, send no text messages and serve any
    account's code from the development endpoint, which is a full account takeover. Local development is unaffected."""
    if hosted and (environment or "").strip().lower() != "production":
        return [
            "This app is running on a hosted server but ENVIRONMENT is not production. Set ENVIRONMENT=production in the host's "
            "environment settings. Refusing to start: in this state sign-in codes are written to the log and any account's code "
            "can be read from the development endpoint."
        ]
    return []


def warnings_for(*, environment: str, fernet_key: str) -> list[str]:
    """Problems that leave the app running but part of it broken."""
    notes: list[str] = []
    if (environment or "").strip().lower() == "production" and not fernet_key:
        notes.append(
            "FERNET_SECRET_KEY is not set. Connecting a platform, publishing and refreshing platform keys will fail until it is set."
        )
    return notes
