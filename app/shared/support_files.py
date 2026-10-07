"""Support attachments: validate, store privately, hand out short-lived links.

What the "scan" is: a check that the file really is the type it claims (the
first bytes must match a PNG, JPEG, WebP or PDF signature; text and log files
must be plain UTF-8 with no binary content). It is not an antivirus. Anything
that fails is blocked and never attached. The allow-list only permits image,
PDF and plain-text types, none of which run code when opened.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from app.db.mongo import support_files
from app.shared import storage
from app.shared.support_rules import (
    ALLOWED_ATTACHMENT_TYPES,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_MESSAGE,
    MAX_VIDEO_BYTES,
    SIGNED_URL_SECONDS,
    VIDEO_EXTENSIONS,
)

logger = logging.getLogger(__name__)


class AttachmentRejected(Exception):
    """A file the member should be told about, with a message that reads well."""


def _clean_name(filename: str) -> str:
    base = re.split(r"[\/]", filename or "file")[-1]
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip()
    return (base or "file")[:120]


def _extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _looks_like(ext: str, head: bytes, data: bytes) -> bool:
    if ext == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in ("jpg", "jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == "webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext == "pdf":
        return head.startswith(b"%PDF-")
    if ext in ("mp4", "mov"):
        return head[4:8] == b"ftyp"
    if ext == "webm":
        return head.startswith(bytes([0x1A, 0x45, 0xDF, 0xA3]))
    if ext in ("txt", "log"):
        sample = data[:65536]
        if b"\x00" in sample:
            return False
        try:
            sample.decode("utf-8")
        except UnicodeDecodeError:
            # A multi-byte character cut by the sample boundary is fine.
            try:
                sample[:-4].decode("utf-8")
            except UnicodeDecodeError:
                return False
        return True
    return False


def validate(filename: str, declared_mime: Optional[str], data: bytes) -> tuple[str, str]:
    """Return (clean_name, mime) or raise AttachmentRejected."""
    name = _clean_name(filename)
    ext = _extension(name)
    if ext not in ALLOWED_ATTACHMENT_TYPES:
        raise AttachmentRejected("That file type isn't supported. You can attach images, videos, PDFs and text files.")
    if len(data) == 0:
        raise AttachmentRejected("That file is empty.")
    limit = MAX_VIDEO_BYTES if ext in VIDEO_EXTENSIONS else MAX_ATTACHMENT_BYTES
    if len(data) > limit:
        raise AttachmentRejected(f"That file is too large. The limit is {limit // (1024 * 1024)} MB.")
    mime = (declared_mime or "").split(";")[0].strip().lower()
    allowed = ALLOWED_ATTACHMENT_TYPES[ext]
    if mime and mime not in allowed:
        raise AttachmentRejected("That file doesn't match its type, so we couldn't accept it.")
    if not _looks_like(ext, data[:16], data):
        raise AttachmentRejected("That file doesn't match its type, so we couldn't accept it.")
    return name, mime or sorted(allowed)[0]


async def store(*, uploader_id: str, uploader_type: str, workspace_id: str, filename: str,
                declared_mime: Optional[str], data: bytes) -> dict:
    """Validate and store one file privately. Raises AttachmentRejected."""
    name, mime = validate(filename, declared_mime, data)
    file_id = str(uuid4())
    ext = _extension(name)
    kind = "video" if ext in VIDEO_EXTENSIONS else "raw"
    try:
        storage_key = await asyncio.to_thread(storage.upload_private_file, data, workspace_id or "staff", file_id, kind)
    except Exception:
        logger.warning("Support attachment upload failed", exc_info=True)
        raise AttachmentRejected("We couldn't save that file right now. Try again in a moment.")
    doc = {
        "id": file_id,
        "workspace_id": workspace_id,
        "uploaded_by": uploader_id,
        "uploader_type": uploader_type,  # "member" | "staff"
        "ticket_id": None,
        "name": name,
        "mime": mime,
        "size": len(data),
        "storage_key": storage_key,
        "resource_type": kind,
        "format": ext,
        "scan_status": "clean",
        "created_at": datetime.now(timezone.utc),
    }
    await support_files.insert_one(doc)
    return doc


async def claim(file_ids: list[str], *, uploader_id: str, ticket_id: str) -> list[dict]:
    """Attach previously uploaded files to a message on ``ticket_id``.

    Only files the caller uploaded, that passed the check, and that are not
    already on a different ticket. Returns the attachment entries to store on
    the message. Raises AttachmentRejected."""
    if not file_ids:
        return []
    if len(set(file_ids)) != len(file_ids):
        raise AttachmentRejected("The same file was added twice.")
    if len(file_ids) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise AttachmentRejected(f"You can attach up to {MAX_ATTACHMENTS_PER_MESSAGE} files at a time.")
    docs = await support_files.find({"id": {"$in": file_ids}, "uploaded_by": uploader_id}).to_list(len(file_ids))
    by_id = {d["id"]: d for d in docs}
    entries: list[dict] = []
    for fid in file_ids:
        d = by_id.get(fid)
        if d is None or d.get("scan_status") != "clean" or d.get("ticket_id") not in (None, ticket_id):
            raise AttachmentRejected("One of the files can't be attached. Remove it and try again.")
        entries.append({"file_id": fid, "name": d["name"], "mime": d["mime"], "size": d["size"]})
    await support_files.update_many({"id": {"$in": file_ids}}, {"$set": {"ticket_id": ticket_id}})
    return entries


async def signed_link(file_doc: dict) -> dict:
    url = await asyncio.to_thread(
        storage.signed_private_url, file_doc["storage_key"], SIGNED_URL_SECONDS,
        file_doc.get("resource_type", "raw"), file_doc.get("format", ""),
    )
    return {"url": url, "name": file_doc["name"], "mime": file_doc["mime"], "expires_in": SIGNED_URL_SECONDS}
