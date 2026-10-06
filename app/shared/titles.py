"""Names the member gives their own work (a text run, a picture project, a recording, a video)."""
from __future__ import annotations

from fastapi import HTTPException

MAX_TITLE_LENGTH = 120


def clean_title(raw: object) -> str:
    """The name to save: extra spaces and line breaks collapsed. Raises a 422 with a plain message when it is empty or too long."""
    text = " ".join(str(raw or "").split())
    if not text:
        raise HTTPException(status_code=422, detail="Add a name.")
    if len(text) > MAX_TITLE_LENGTH:
        raise HTTPException(status_code=422, detail=f"A name can be up to {MAX_TITLE_LENGTH} characters.")
    return text
