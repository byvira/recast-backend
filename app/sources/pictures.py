"""Reading a picture given as a source: a poster, an offer, a price board, a screenshot.

The picture goes through the same content check as every picture first (a refused picture is never read). A vision model then writes down the words that can be
seen and one line about what the picture shows; people in it are described, never named. The shared free vision allowance is small, so a workspace can add a
limited number of pictures a day, and the cost of the allowance is said to the member before anything is read. The picture itself is not kept."""
from __future__ import annotations

import logging
import re
from typing import Optional

from app.sources.items import MAX_TITLE, Item, ReadError, item_id, plain

logger = logging.getLogger(__name__)

#: pictures a workspace may add as sources in one day
DAILY_PICTURES = 10
MAX_PICTURE_BYTES = 8 * 1024 * 1024
MIME = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}

PROMPT = (
    "Read this picture for someone who wants to write social posts about what it shows.\n"
    "Answer with exactly two parts, in this form:\n"
    "TITLE: a short title of at most 8 words\n"
    "TEXT: every word, number and price that can be read in the picture, exactly as written, then one plain sentence about what the picture shows.\n"
    "If people appear, describe what they are doing and wearing. Never say who they are, even if they look familiar or a name is written near them.\n"
    "If nothing can be read and the picture shows nothing useful, answer TITLE: Nothing to read and TEXT: empty."
)


def mime_of(data: bytes) -> str:
    """The picture type from its first bytes, or an empty string for anything this reader does not take."""
    if data.startswith(b"\xff\xd8\xff"):
        return MIME["jpeg"]
    if data.startswith(b"\x89PNG"):
        return MIME["png"]
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return MIME["webp"]
    return ""


def _parts(answer: str) -> tuple[str, str]:
    text = (answer or "").strip()
    title = re.search(r"TITLE:\s*(.+)", text)
    body = re.search(r"TEXT:\s*(.*)", text, re.DOTALL)
    return (title.group(1).strip() if title else ""), (body.group(1).strip() if body else text)


async def read_picture(data: bytes, filename: str, workspace_id: str, *, describe=None, screen=None) -> tuple[str, list[Item]]:
    """(a title, one item) for a picture. `describe(prompt, bytes, mime)` and `screen(bytes, mime, workspace_id)` are the vision model and the content check, replaceable in tests.
    Raises `ReadError` for a picture that is too large, not a supported type, refused by the content check, or has nothing to read."""
    if len(data) > MAX_PICTURE_BYTES:
        raise ReadError("too_large", f"That picture is larger than {MAX_PICTURE_BYTES // (1024 * 1024)} MB. Use a smaller one.")
    mime = mime_of(data)
    if not mime:
        raise ReadError("unsupported", "Pictures can be JPG, PNG or WebP.")
    if screen is None:
        from app.agents.content_guard.media import assert_image_ok

        async def screen(blob: bytes, kind: str, workspace: str) -> None:  # noqa: F811
            await assert_image_ok(blob, kind, workspace_id=workspace, where="source")
    try:
        await screen(data, mime, workspace_id)
    except ReadError:
        raise
    except Exception as exc:  # noqa: BLE001 - the content check refuses with its own plain message
        raise ReadError("picture_refused", str(getattr(exc, "detail", None) or getattr(exc, "message", None) or "That picture cannot be used.")) from exc
    if describe is None:
        from app.shared.llm import call_vision, set_usage_workspace

        set_usage_workspace(workspace_id)
        describe = call_vision
    try:
        answer = await describe(PROMPT, data, mime)
    except Exception as exc:  # noqa: BLE001 - a vision provider problem is a retry-later message, never a crash
        logger.info("A picture source could not be read: %s", type(exc).__name__)
        raise ReadError("vision_unavailable", "The picture reader is busy right now. Try again in a few minutes.") from exc
    title, text = _parts(str(answer or ""))
    if not text or text.lower().strip(". ") == "empty":
        raise ReadError("nothing_to_read", "Nothing could be read in that picture. Try a clearer one, or one with words in it.")
    name = plain(title, MAX_TITLE) or re.sub(r"\.[A-Za-z0-9]+$", "", filename or "Picture")[:80] or "Picture"
    return name, [Item(id=item_id("", f"picture:{len(data)}:{name}", name), title=name, text=plain(text, 2000), kind="image")]


def allowance_left(used_today: int) -> int:
    return max(0, DAILY_PICTURES - used_today)
