"""Replies sent by email to a support email become messages on the ticket.

A reply arrives as a signed message from Resend ("email.received"). It is added only when all of this holds: the signature is
right, the ticket can be found from the address it was sent to (or the "#number" in its subject), the sender is the address of the
person who filed the ticket, that person's support is not paused, the ticket is open and they are under the hourly message limit.
Anything else is dropped without telling the sender anything about the ticket. A closed ticket or a reached limit is told to the
member inside the app. The quoted earlier messages at the bottom of a reply are cut off (English and the common European and
Indian ways of writing "On <date>, <name> wrote:"). Attachments go through the same file check as the Support page; any that
are refused are named in a note under the message.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Awaitable, Callable, Optional

import httpx
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.db.mongo import support_inbound_emails, support_tickets, users
from app.shared import support_rules as rules
from app.shared.support_notify import notify_member
from app.shared.support_reply import ReplyConflict, add_member_message, member_hit_message_limit

logger = logging.getLogger(__name__)

SIGNATURE_TOLERANCE_SECONDS = 300
_TICKET_IN_ADDRESS = re.compile(r"\+([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})@")
_NUMBER_IN_SUBJECT = re.compile(r"#(\d{1,9})\b")
_WROTE = re.compile(r"^On .{3,300} wrote:\s*$")
_ORIGINAL = re.compile(r"^-{2,}\s*(Original Message|Forwarded message)\s*-{2,}\s*$", re.IGNORECASE)
# "<date>, <name> <address> <verb>:" in any language: an address in angle brackets and a colon at the end of the line.
_ATTRIBUTION = re.compile(r"^.{0,300}<[^<>\s]+@[^<>\s]+>.{0,60}[:：]\s*$")
_ADDRESS_LINE = re.compile(r"^<?[^<>\s]+@[^<>\s]+>?.{0,60}[:：]\s*$")
# A forwarded header block (Outlook and others): "From:" followed by the date or recipients.
_HEADER_START = re.compile(r"^(From|De|Von|Da|Van)\s*:", re.IGNORECASE)
_HEADER_NEXT = re.compile(r"^(Sent|Date|To|Subject|Envoy|Gesendet|Enviado|Fecha|Para|An|Objet|Betreff|Asunto)\s*:", re.IGNORECASE)
_RULE = re.compile(r"^[_\-=]{10,}$")

Fetcher = Callable[[str], Awaitable[dict]]
FilesFetcher = Callable[[str], Awaitable[list]]


def verify_signature(body: bytes, headers: dict, secret: str, now: Optional[float] = None) -> bool:
    """Resend signs each message (Svix style): HMAC-SHA256 of "id.timestamp.body" with the secret, sent as "v1,<base64>"."""
    msg_id = headers.get("svix-id", "")
    stamp = headers.get("svix-timestamp", "")
    signatures = headers.get("svix-signature", "")
    if not (secret and msg_id and stamp and signatures):
        return False
    try:
        if abs((now if now is not None else time.time()) - int(stamp)) > SIGNATURE_TOLERANCE_SECONDS:
            return False
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    except Exception:  # noqa: BLE001
        return False
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(expected, part.split(",", 1)[1]) for part in signatures.split() if "," in part)


def reply_text(text: str) -> str:
    """What the person wrote, without the earlier messages quoted under it."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    kept: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        following = [ln.strip() for ln in lines[i + 1:i + 4]]
        wraps = bool(following) and stripped and "@" not in stripped and bool(_ADDRESS_LINE.match(following[0])) and not stripped.endswith((":", "："))
        wraps = wraps or (stripped.startswith("On ") and bool(following) and following[0].endswith("wrote:") and "wrote:" not in stripped)
        header = bool(_HEADER_START.match(stripped)) and any(_HEADER_NEXT.match(ln) for ln in following)
        if _WROTE.match(stripped) or _ORIGINAL.match(stripped) or _ATTRIBUTION.match(stripped) or _RULE.match(stripped) or wraps or header or stripped.startswith(">"):
            break
        kept.append(line)
    return "\n".join(kept).strip()[: rules.MAX_MESSAGE_CHARS]


def _plain_from_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html or "")
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
    return re.sub(r"<[^>]+>", "", html)


async def fetch_received(email_id: str) -> dict:
    """The full message (the signed notice carries only who and what; the words come from a second request)."""
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.get(
            f"https://api.resend.com/emails/receiving/{email_id}", headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"}
        )
    res.raise_for_status()
    return res.json()


async def fetch_attachments(email_id: str) -> list[dict]:
    """The attachments of a received email, each with a filename, type and a time-limited download link, and the file's bytes."""
    headers = {"Authorization": f"Bearer {settings.RESEND_API_KEY}"}
    found: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        listing = await client.get(f"https://api.resend.com/emails/receiving/{email_id}/attachments", headers=headers)
        listing.raise_for_status()
        for item in (listing.json().get("data") or [])[: rules.MAX_ATTACHMENTS_PER_MESSAGE]:
            url = item.get("download_url")
            if not url:
                continue
            got = await client.get(url)
            got.raise_for_status()
            found.append({"filename": item.get("filename") or "file", "content_type": item.get("content_type"), "data": got.content})
    return found


async def _attach(ticket: dict, owner: dict, email_id: str, fetch_files: FilesFetcher) -> tuple[list[dict], list[str]]:
    """(attachment entries for the message, names of the files that were refused). A failure to reach the files means none are
    added and none are named as refused: the note under the message says they could not be added."""
    from app.shared import support_files

    try:
        files = await fetch_files(email_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not fetch the attachments of a support email reply: %s", exc)
        return [], ["(the attachments could not be read)"]
    stored: list[str] = []
    refused: list[str] = []
    for item in files:
        try:
            doc = await support_files.store(
                uploader_id=owner["id"], uploader_type="member", workspace_id=ticket.get("workspace_id"),
                filename=item["filename"], declared_mime=item.get("content_type"), data=item["data"],
            )
            stored.append(doc["id"])
        except support_files.AttachmentRejected:
            refused.append(item["filename"])
    entries = await support_files.claim(stored, uploader_id=owner["id"], ticket_id=ticket["id"]) if stored else []
    return entries, refused


async def _find_ticket(data: dict) -> Optional[dict]:
    for address in data.get("to") or []:
        found = _TICKET_IN_ADDRESS.search(str(address))
        if found:
            return await support_tickets.find_one({"id": found.group(1).lower()})
    number = _NUMBER_IN_SUBJECT.search(str(data.get("subject") or ""))
    if number:
        return await support_tickets.find_one({"number": int(number.group(1))})
    return None


async def _apply(data: dict, fetch: Fetcher, fetch_files: FilesFetcher) -> str:
    ticket = await _find_ticket(data)
    if not ticket:
        return "no_ticket"
    owner = await users.find_one({"id": ticket["created_by"]}, {"id": 1, "name": 1, "email": 1, "support_muted": 1})
    sender = parseaddr(str(data.get("from") or ""))[1].strip().lower()
    if not owner or not sender or sender != str(owner.get("email") or "").strip().lower():
        return "sender_mismatch"
    if owner.get("support_muted"):
        return "muted"
    if ticket["status"] == "closed":
        await notify_member(
            ticket, "email_reply_not_added", "Your email reply was not added",
            "This ticket is closed. Open a new ticket and mention this one.",
        )
        return "closed"
    if await member_hit_message_limit(owner["id"]):
        await notify_member(
            ticket, "email_reply_not_added", "Your email reply was not added",
            "You have sent a lot of messages in the last hour. Please wait a little and reply again.",
        )
        return "limit"
    full = await fetch(str(data.get("email_id") or data.get("id")))
    text = reply_text(full.get("text") or _plain_from_html(full.get("html") or ""))
    if not text:
        return "empty"
    entries: list[dict] = []
    if data.get("attachments"):
        entries, refused = await _attach(ticket, owner, str(data.get("email_id")), fetch_files)
        if refused:
            text = (text + f"\n\n(Not added from the email: {', '.join(refused)}. You can upload files on the ticket page.)")[: rules.MAX_MESSAGE_CHARS]
    for _ in range(3):
        try:
            await add_member_message(ticket, user=owner, text=text, attachments=entries, via="email")
            return "added"
        except ReplyConflict:
            ticket = await support_tickets.find_one({"id": ticket["id"]})
            if not ticket or ticket["status"] == "closed":
                return "closed"
    raise RuntimeError("The ticket kept changing; the reply will be tried again")


async def handle_event(event: dict, fetch: Fetcher = fetch_received, fetch_files: FilesFetcher = fetch_attachments) -> str:
    """Apply one signed message. Returns what happened. Raises when it should be tried again (Resend retries on an error)."""
    if event.get("type") != "email.received":
        return "ignored"
    data = event.get("data") or {}
    email_id = str(data.get("email_id") or data.get("id") or "")
    if not email_id:
        return "ignored"
    try:
        await support_inbound_emails.insert_one({"email_id": email_id, "at": datetime.now(timezone.utc), "outcome": "working"})
    except DuplicateKeyError:
        return "duplicate"
    try:
        outcome = await _apply({**data, "email_id": email_id}, fetch, fetch_files)
    except BaseException:
        # not finished: forget it, so the retry Resend sends is not mistaken for a duplicate
        await support_inbound_emails.delete_one({"email_id": email_id})
        raise
    await support_inbound_emails.update_one({"email_id": email_id}, {"$set": {"outcome": outcome}})
    return outcome
