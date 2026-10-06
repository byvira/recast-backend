"""Bluesky video. A video does not go through the normal blob upload: it is sent to Bluesky's video service with a short lived "service
auth" token from the member's own server, processed there, and the finished blob is then named in the post.

Steps: find the member's server from their DID, ask it for a service auth token for the video service, upload the file, wait for the
processing job to finish. Built from Bluesky's documented flow; not yet confirmed against a live account, so any failure leaves the
post to go out as text with a plain note.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional
from urllib.parse import urlsplit

import httpx

VIDEO_SERVICE = "https://video.bsky.app/xrpc"
VIDEO_AUDIENCE_LXM = "com.atproto.repo.uploadBlob"
PLC_DIRECTORY = "https://plc.directory"
MAX_BYTES = 300 * 1024 * 1024
POLL_SECONDS = 3.0
MAX_POLLS = 60  # about three minutes


class VideoError(Exception):
    """The video could not be posted; the message is plain enough to show to the member."""


async def pds_audience(client: httpx.AsyncClient, did: str) -> str:
    """The DID of the member's own server, from their DID document. Only did:plc accounts are looked up."""
    if not did.startswith("did:plc:"):
        raise VideoError("Video can't be sent from this kind of Bluesky account yet.")
    document = await client.get(f"{PLC_DIRECTORY}/{did}")
    document.raise_for_status()
    for service in document.json().get("service", []):
        if service.get("id", "").endswith("atproto_pds") and service.get("serviceEndpoint"):
            host = urlsplit(service["serviceEndpoint"]).hostname
            if host:
                return f"did:web:{host}"
    raise VideoError("Couldn't find the server for this Bluesky account.")


async def upload_video(
    client: httpx.AsyncClient, *, pds_base: str, access_token: str, did: str, video: bytes, mime_type: str, name: str,
    sleep=asyncio.sleep,
) -> dict:
    """Uploads the video and returns its finished blob. Raises VideoError (plain message) or httpx errors."""
    if len(video) > MAX_BYTES:
        raise VideoError("Bluesky takes videos up to 300 MB.")
    audience = await pds_audience(client, did)
    auth = await client.get(
        f"{pds_base}/com.atproto.server.getServiceAuth",
        params={"aud": audience, "lxm": VIDEO_AUDIENCE_LXM, "exp": int(time.time()) + 30 * 60},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    auth.raise_for_status()
    service_token = auth.json()["token"]

    sent = await client.post(
        f"{VIDEO_SERVICE}/app.bsky.video.uploadVideo",
        params={"did": did, "name": name[:100]},
        content=video,
        headers={"Authorization": f"Bearer {service_token}", "Content-Type": mime_type or "video/mp4"},
    )
    # A video the service already has comes back as a conflict with the job's details.
    if sent.status_code not in (200, 409):
        sent.raise_for_status()
    job = sent.json()
    job_id = job.get("jobId") or (job.get("jobStatus") or {}).get("jobId")
    if not job_id:
        raise VideoError("Bluesky didn't accept the video.")
    state = job.get("jobStatus") or job
    for _ in range(MAX_POLLS):
        if state.get("state") == "JOB_STATE_COMPLETED" and state.get("blob"):
            return state["blob"]
        if state.get("state") == "JOB_STATE_FAILED":
            raise VideoError(state.get("error") or "Bluesky couldn't process the video.")
        await sleep(POLL_SECONDS)
        status = await client.get(f"{VIDEO_SERVICE}/app.bsky.video.getJobStatus", params={"jobId": job_id})
        status.raise_for_status()
        state = status.json().get("jobStatus", {})
    raise VideoError("Bluesky is still processing the video. Try again in a few minutes.")


def video_embed(blob: dict, alt: Optional[str]) -> dict:
    embed: dict = {"$type": "app.bsky.embed.video", "video": blob}
    if alt:
        embed["alt"] = alt[:10000]
    return embed
