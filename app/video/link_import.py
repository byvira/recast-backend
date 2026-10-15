"""Bringing a video in from a shared link (Dropbox or Google Drive). Only links that anyone with the link can open are used: no sign-in to the
member's drive is asked for, nothing is read except the one file the link points at. The link is turned into a direct download address and
storage fetches the file itself, so the video never passes through this server."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

DROPBOX_HOSTS = ("www.dropbox.com", "dropbox.com", "dl.dropboxusercontent.com")
DRIVE_HOSTS = ("drive.google.com", "docs.google.com")
#: Hosts the direct address may end on after a redirect.
FETCH_HOSTS = ("dl.dropboxusercontent.com", "uc.dropbox.com", "www.dropbox.com", "dropbox.com", "drive.google.com", "drive.usercontent.google.com")
_FILE_ID = re.compile(r"^[A-Za-z0-9_-]{10,100}$")


class LinkError(ValueError):
    """A link that cannot be used, with a plain reason."""


def direct_address(link: str) -> tuple[str, str]:
    """(provider, direct download address) for a shared link, or raises LinkError. Folders and links that need a sign-in are refused."""
    link = (link or "").strip()
    if len(link) > 1000 or not link.startswith("https://"):
        raise LinkError("Paste the full https link from Dropbox or Google Drive.")
    parts = urlsplit(link)
    host = (parts.hostname or "").lower()
    if host in DROPBOX_HOSTS:
        if "/scl/fo/" in parts.path or parts.path.startswith("/sh/") and "/AA" not in parts.path and False:
            raise LinkError("That is a folder. Share one video file and paste its link.")
        if "/scl/fo/" in parts.path:
            raise LinkError("That is a folder. Share one video file and paste its link.")
        query = {k: v[0] for k, v in parse_qs(parts.query).items() if k not in ("dl", "raw")}
        query["dl"] = "1"
        return "dropbox", urlunsplit(("https", "www.dropbox.com" if host != "dl.dropboxusercontent.com" else host, parts.path, urlencode(query), ""))
    if host in DRIVE_HOSTS:
        if "/folders/" in parts.path or "/drive/folders" in parts.path:
            raise LinkError("That is a folder. Share one video file and paste its link.")
        match = re.search(r"/file/d/([A-Za-z0-9_-]+)", parts.path)
        file_id = match.group(1) if match else (parse_qs(parts.query).get("id") or [""])[0]
        if not _FILE_ID.match(file_id or ""):
            raise LinkError("That does not look like a link to a single Google Drive file.")
        return "drive", f"https://drive.google.com/uc?export=download&id={file_id}"
    raise LinkError("Only Dropbox and Google Drive links are supported. Share the file so anyone with the link can view it.")


def fetch_host_ok(url: str) -> bool:
    """Whether an address the download was redirected to is still one of the providers' own."""
    host = (urlsplit(url).hostname or "").lower()
    return url.startswith("https://") and (host in FETCH_HOSTS or host.endswith(".dropboxusercontent.com") or host.endswith(".googleusercontent.com"))


def name_from_headers(content_disposition: Optional[str], fallback: str) -> str:
    """The file name the provider gave, kept to plain characters."""
    name = ""
    if content_disposition:
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", content_disposition, re.IGNORECASE)
        if m:
            name = m.group(1)
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name.strip())[:120]
    return name or fallback
