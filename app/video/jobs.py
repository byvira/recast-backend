"""Render jobs for the video worker.

A job is one row in `video_jobs`. The backend owns the truth: a worker claims a job under a lease, keeps it alive with
heartbeats and reports the result. A worker that goes quiet loses the job to the queue. Nothing finished is shown to a member
until it has passed every check in `complete` (the quality report, size, storage checksum and the Content Guard on frames).

States: queued, claimed, done, failed, cancelled, blocked (the Content Guard held the video back).
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

import httpx
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.db.mongo import get_db, media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.video import storage_params

logger = logging.getLogger(__name__)

CONTRACT = 1
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
INPUT_KINDS = ("video", "audio", "image")
OUTPUT_KEYS = ("filename", "segment_seconds", "crf", "preset", "audio_bitrate", "lufs")
# A worker that has not asked for work for this long is reported as offline.
WORKER_OFFLINE_AFTER = timedelta(minutes=5)
MAX_FRAMES = 12
# Failures that can never succeed on a second try, whatever the worker says.
NEVER_RETRY = ("bad_job", "bad_input", "too_long", "quality_check_failed", "delivery_mismatch")


class JobError(Exception):
    """A refusal with an HTTP status and a message that is safe to show."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class LeaseLost(Exception):
    """The job is no longer held by the worker that is calling."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _jobs():
    return get_db()["video_jobs"]


def _state():
    return get_db()["video_worker_state"]


def lease_seconds() -> int:
    return max(30, int(settings.VIDEO_WORKER_LEASE_SECONDS))


# ── what a member or staff sees ──────────────────────────────────────────────────

def public(doc: Optional[dict]) -> Optional[dict]:
    """The job as the API returns it: no lease secret, no source addresses, no edit list."""
    if not doc:
        return None
    out = {k: doc.get(k) for k in (
        "id", "workspace_id", "label", "status", "attempts", "duration_s", "media_id", "error", "error_code",
        "progress", "cancel_requested", "created_at", "claimed_at", "finished_at",
    )}
    for field in ("created_at", "claimed_at", "finished_at"):
        value = _aware(out.get(field))
        out[field] = value.isoformat().replace("+00:00", "Z") if value else None
    if doc.get("output"):
        out["output"] = {k: doc["output"].get(k) for k in ("url", "bytes", "duration_s", "width", "height")}
    return out


# ── making a job ─────────────────────────────────────────────────────────────────

def _validate_request(inputs: Any, edit: Any) -> float:
    """The checks that protect the backend. The worker checks the whole contract again and refuses anything outside it."""
    if not isinstance(inputs, dict) or not inputs:
        raise JobError(400, "A render needs at least one source file.")
    for key, item in inputs.items():
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise JobError(400, "A source name may only use letters, numbers, - and _.")
        if not isinstance(item, dict) or not str(item.get("url", "")).startswith("https://"):
            raise JobError(400, "Every source needs a secure (https) address.")
        if item.get("kind", "video") not in INPUT_KINDS:
            raise JobError(400, "A source must be a video, an audio file or a picture.")
    if not isinstance(edit, dict):
        raise JobError(400, "The edit is missing.")
    fps = edit.get("fps") or {}
    num, den = fps.get("num"), fps.get("den", 1)
    if not (isinstance(num, int) and isinstance(den, int) and num > 0 and den > 0 and 1 <= num / den <= 120):
        raise JobError(400, "The frame rate must be between 1 and 120 frames a second.")
    width, height, frames = edit.get("width"), edit.get("height"), edit.get("duration_f")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (width, height, frames)):
        raise JobError(400, "The picture size and length must be whole numbers.")
    if not (16 <= width <= 4096 and 16 <= height <= 4096 and width % 2 == 0 and height % 2 == 0):
        raise JobError(400, "The picture size must be even and between 16 and 4096 pixels on each side.")
    if frames < 1:
        raise JobError(400, "The video has no length.")
    seconds = frames * den / num
    cap = int(settings.VIDEO_MAX_OUTPUT_SECONDS)
    if seconds > cap:
        raise JobError(400, f"That video is {seconds / 60:.0f} minutes long. One video can be at most {cap // 60} minutes for now.")
    for clip in (edit.get("video") or []) + (edit.get("audio") or []):
        if not isinstance(clip, dict) or clip.get("src") not in inputs:
            raise JobError(400, "A clip points at a source that is not part of this render.")
    return seconds


async def enqueue(
    *, workspace_id: str, user_id: str, inputs: dict, edit: dict, output: Optional[dict] = None,
    label: str = "", idem_key: Optional[str] = None,
) -> dict:
    """Queue one render. The same idempotency key in the same workspace returns the job already made."""
    seconds = _validate_request(inputs, edit)
    if idem_key:
        existing = await _jobs().find_one({"workspace_id": workspace_id, "idem_key": idem_key})
        if existing:
            return public(existing)
    now = _now()
    doc = {
        "id": str(uuid4()), "workspace_id": workspace_id, "user_id": user_id, "label": (label or "")[:120],
        "status": "queued", "attempts": 0, "inputs": inputs, "edit": edit,
        "output": {k: v for k, v in (output or {}).items() if k in OUTPUT_KEYS},
        "duration_s": round(seconds, 3), "created_at": now, "lease_token": None, "lease_expires_at": None,
        "cancel_requested": False, "progress": {},
    }
    if idem_key:
        doc["idem_key"] = idem_key
    try:
        await _jobs().insert_one(dict(doc))
    except DuplicateKeyError:
        return public(await _jobs().find_one({"workspace_id": workspace_id, "idem_key": idem_key}))
    return public(doc)


# ── the worker's side ────────────────────────────────────────────────────────────

async def touch_worker(worker_id: str, version: str, capabilities: dict) -> None:
    await _state().update_one(
        {"_id": "render_worker"},
        {"$set": {"worker_id": worker_id[:80], "version": version[:40], "capabilities": capabilities, "last_seen": _now()}},
        upsert=True,
    )


async def reap_expired() -> None:
    """Jobs whose worker stopped reporting go back to the queue; ones that are out of attempts, or that the member
    cancelled, are closed."""
    now = _now()
    expired = {"status": "claimed", "lease_expires_at": {"$lt": now}}
    closed = {"lease_token": None, "lease_expires_at": None, "finished_at": now}
    await _jobs().update_many(
        {**expired, "cancel_requested": True},
        {"$set": {"status": "cancelled", **closed}},
    )
    await _jobs().update_many(
        {**expired, "attempts": {"$gte": settings.VIDEO_WORKER_MAX_ATTEMPTS}},
        {"$set": {"status": "failed", "error_code": "worker_lost",
                  "error": "The video could not be finished after several tries. Please try again.", **closed}},
    )
    await _jobs().update_many(
        {**expired, "attempts": {"$lt": settings.VIDEO_WORKER_MAX_ATTEMPTS}},
        {"$set": {"status": "queued", "lease_token": None, "lease_expires_at": None, "progress": {}}},
    )


def _wire(doc: dict) -> dict:
    return {
        "id": doc["id"], "kind": "render", "contract": CONTRACT, "lease_token": doc["lease_token"],
        "lease_seconds": lease_seconds(), "inputs": doc["inputs"], "edit": doc["edit"],
        "output": doc.get("output") or {}, "limits": {"max_output_seconds": int(settings.VIDEO_MAX_OUTPUT_SECONDS)},
    }


async def claim(worker_id: str, version: str, capabilities: dict) -> Optional[dict]:
    """Hand the oldest suitable job to this worker, atomically. A workspace that has no render going is served before one that
    already has one, so a single member cannot hold the queue."""
    await touch_worker(worker_id, version, capabilities)
    await reap_expired()
    cap = capabilities.get("max_output_seconds")
    cap = int(cap) if isinstance(cap, (int, float)) and cap > 0 else int(settings.VIDEO_MAX_OUTPUT_SECONDS)
    base = {"status": "queued", "duration_s": {"$lte": cap}, "cancel_requested": {"$ne": True}}
    busy = await _jobs().distinct("workspace_id", {"status": "claimed"})
    now = _now()
    update = {
        "$set": {"status": "claimed", "lease_token": secrets.token_urlsafe(18), "lease_expires_at": now + timedelta(seconds=lease_seconds()),
                 "worker_id": worker_id[:80], "claimed_at": now, "progress": {}},
        "$inc": {"attempts": 1},
    }
    for flt in ({**base, "workspace_id": {"$nin": busy}}, base):
        doc = await _jobs().find_one_and_update(flt, update, sort=[("created_at", 1)], return_document=ReturnDocument.AFTER)
        if doc:
            return _wire(doc)
    return None


def _progress(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    if isinstance(raw.get("stage"), str):
        out["stage"] = raw["stage"][:120]
    for key in ("segments_done", "segments_total"):
        if isinstance(raw.get(key), int) and not isinstance(raw.get(key), bool) and raw[key] >= 0:
            out[key] = raw[key]
    if isinstance(raw.get("fraction"), (int, float)):
        out["fraction"] = max(0.0, min(1.0, float(raw["fraction"])))
    return out


async def _held(job_id: str, lease_token: str) -> dict:
    doc = await _jobs().find_one({"id": job_id, "status": "claimed", "lease_token": lease_token})
    if not doc or not lease_token:
        raise LeaseLost()
    return doc


async def heartbeat(job_id: str, lease_token: str, progress: Any) -> dict:
    doc = await _jobs().find_one_and_update(
        {"id": job_id, "status": "claimed", "lease_token": lease_token or "\0"},
        {"$set": {"lease_expires_at": _now() + timedelta(seconds=lease_seconds()), "progress": _progress(progress)}},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise LeaseLost()
    return {"cancel": bool(doc.get("cancel_requested")), "lease_seconds": lease_seconds()}


async def upload_params(job_id: str, lease_token: str, purpose: str, filename: str, nbytes: Any) -> dict:
    doc = await _held(job_id, lease_token)
    if not isinstance(nbytes, int) or isinstance(nbytes, bool):
        raise JobError(400, "bytes must be a whole number.")
    try:
        return storage_params.upload_params(
            workspace_id=doc["workspace_id"], job_id=job_id, purpose=purpose, filename=str(filename or "file"), nbytes=nbytes,
        )
    except ValueError as exc:
        raise JobError(400, str(exc))


async def _remote_size(url: str) -> Optional[int]:
    """The size of a stored file as the storage service reports it. None when it cannot be read."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for _ in range(2):
            try:
                head = await client.head(url)
                if head.status_code < 400 and head.headers.get("content-length", "").isdigit():
                    return int(head.headers["content-length"])
                probe = await client.get(url, headers={"Range": "bytes=0-0"})
                total = probe.headers.get("content-range", "").rpartition("/")[2]
                if probe.status_code in (200, 206) and total.isdigit():
                    return int(total)
            except httpx.HTTPError as exc:
                logger.warning("Could not read the size of %s: %s", url, exc)
    return None


async def _fetch_frame(url: str) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
        if resp.status_code == 200 and 0 < len(resp.content) <= storage_params.MAX_FRAME_BYTES:
            return resp.content
    except httpx.HTTPError as exc:
        logger.warning("Could not read a sample frame: %s", exc)
    return None


async def _forget_files(urls: list[str]) -> None:
    from app.shared.storage import delete_file_by_url

    for url in urls:
        await delete_file_by_url(url)


def _delivery_problem(doc: dict, video: Optional[dict], report: Any) -> Optional[str]:
    """Why a report cannot be accepted, or None. The first four checks of the delivery rules; size and frames come after."""
    if not isinstance(report, dict) or report.get("passed") is not True:
        return "the quality report does not say the video passed"
    gate = report.get("gate_version")
    if not isinstance(gate, int) or gate < int(settings.VIDEO_GATE_VERSION_MIN):
        return "the quality report is from an older checker"
    if not video or not isinstance(video.get("url"), str) or not storage_params.is_our_storage_url(video["url"]):
        return "the finished file is not in our storage"
    if not isinstance(video.get("bytes"), int) or video["bytes"] <= 0:
        return "the finished file has no size"
    edit = doc["edit"]
    fps = edit["fps"]
    tolerance = max(0.1, 2 * fps.get("den", 1) / fps["num"])
    length = report.get("duration_s")
    if not isinstance(length, (int, float)) or abs(float(length) - float(doc["duration_s"])) > tolerance:
        return "the finished video is not the length that was asked for"
    if report.get("width") != edit["width"] or report.get("height") != edit["height"]:
        return "the finished video is not the size that was asked for"
    return None


async def _end(doc: dict, status: str, code: str, message: str, detail: Any = None) -> None:
    await _jobs().update_one(
        {"id": doc["id"], "lease_token": doc["lease_token"]},
        {"$set": {"status": status, "error_code": code, "error": message, "error_detail": detail, "finished_at": _now(),
                  "lease_token": None, "lease_expires_at": None}},
    )


async def complete(job_id: str, lease_token: str, outputs: Any, report: Any) -> dict:
    """Accept a finished render. The video is shown to nobody until the report, the size, the stored file and the sample
    frames all check out; anything missing ends the job with a plain message and keeps the evidence for staff."""
    doc = await _held(job_id, lease_token)
    outputs = outputs if isinstance(outputs, list) else []
    video = next((o for o in outputs if isinstance(o, dict) and o.get("role") == "video"), None)
    frames = [o for o in outputs if isinstance(o, dict) and o.get("role") == "frame" and isinstance(o.get("url"), str)][:MAX_FRAMES]
    stored = [o["url"] for o in ([video] if video else []) + frames if isinstance(o.get("url"), str) and storage_params.is_our_storage_url(o["url"])]

    problem = _delivery_problem(doc, video, report)
    if not problem:
        size = await _remote_size(video["url"])
        if size is None:
            problem = "the stored file could not be read back"
        elif size != video["bytes"]:
            problem = f"the stored file is {size} bytes, not the {video['bytes']} reported"
    if problem:
        await _forget_files(stored)
        await _end(doc, "failed", "delivery_mismatch", "The video was made but could not be checked, so it was not saved. Please try again.",
                   {"problem": problem, "report": report})
        logger.warning("Render %s refused at delivery: %s", job_id, problem)
        return {"ok": False, "accepted": False}

    from app.agents.content_guard.media import ContentRejected, assert_image_ok

    for frame in frames:
        data = await _fetch_frame(frame["url"]) if storage_params.is_our_storage_url(frame["url"]) else None
        if not data:
            continue
        try:
            await assert_image_ok(data, "image/jpeg", workspace_id=doc["workspace_id"], where="video render")
        except ContentRejected as exc:
            await _forget_files(stored)
            message = exc.detail.get("message") if isinstance(exc.detail, dict) else "This video was held back by the safety check."
            await _end(doc, "blocked", "content_guard", message, {"categories": (exc.detail or {}).get("categories")})
            return {"ok": True, "accepted": False, "held": True}
    await _forget_files([f["url"] for f in frames if storage_params.is_our_storage_url(f["url"])])

    now = _now()
    media = MediaAsset(
        id=str(uuid4()), workspace_id=doc["workspace_id"], kind=MediaKind.VIDEO, url=video["url"], mime_type="video/mp4",
        width=report.get("width"), height=report.get("height"), duration_s=round(float(report["duration_s"]), 2),
        source=MediaSource.ENHANCED, created_by=doc["user_id"], created_at=now, size_bytes=video["bytes"],
    )
    await media_assets.insert_one(media.model_dump())
    await _jobs().update_one(
        {"id": job_id, "lease_token": lease_token},
        {"$set": {"status": "done", "media_id": media.id, "finished_at": now, "lease_token": None, "lease_expires_at": None,
                  "output": {"url": video["url"], "bytes": video["bytes"], "sha256": video.get("sha256"),
                             "duration_s": report.get("duration_s"), "width": report.get("width"), "height": report.get("height")},
                  "report": report, "progress": {"stage": "Done", "fraction": 1.0}}},
    )
    return {"ok": True, "accepted": True, "media_id": media.id}


async def fail(job_id: str, lease_token: str, code: str, message: str, retryable: bool, detail: Any = None) -> dict:
    """The worker could not finish. A retryable failure goes back in the queue until the job is out of attempts.
    A worker that was simply restarted (`worker_stopped`) does not use up an attempt."""
    doc = await _held(job_id, lease_token)
    code = (code or "internal")[:40]
    safe = (message or "The video could not be made. Please try again.")[:300]
    again = bool(retryable) and code not in NEVER_RETRY and doc["attempts"] < settings.VIDEO_WORKER_MAX_ATTEMPTS
    if code == "worker_stopped":
        again = True
    if again:
        update: dict[str, Any] = {"$set": {"status": "queued", "lease_token": None, "lease_expires_at": None, "progress": {},
                                           "last_error": {"code": code, "detail": detail}}}
        if code == "worker_stopped":
            update["$inc"] = {"attempts": -1}
        await _jobs().update_one({"id": job_id, "lease_token": lease_token}, update)
        return {"ok": True, "requeued": True}
    await _end(doc, "failed", code, safe, detail)
    return {"ok": True, "requeued": False}


# ── members and staff ────────────────────────────────────────────────────────────

async def get_job(workspace_id: str, job_id: str) -> Optional[dict]:
    return public(await _jobs().find_one({"id": job_id, "workspace_id": workspace_id}))


async def list_jobs(workspace_id: Optional[str] = None, limit: int = 20) -> list[dict]:
    flt = {"workspace_id": workspace_id} if workspace_id else {}
    docs = await _jobs().find(flt).sort("created_at", -1).to_list(length=max(1, min(limit, 100)))
    return [public(d) for d in docs]


async def cancel_job(workspace_id: str, job_id: str) -> dict:
    """A queued job is cancelled at once; a running one is told to stop on its next heartbeat."""
    now = _now()
    done = await _jobs().find_one_and_update(
        {"id": job_id, "workspace_id": workspace_id, "status": "queued"},
        {"$set": {"status": "cancelled", "cancel_requested": True, "finished_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if done:
        return public(done)
    running = await _jobs().find_one_and_update(
        {"id": job_id, "workspace_id": workspace_id, "status": "claimed"},
        {"$set": {"cancel_requested": True}}, return_document=ReturnDocument.AFTER,
    )
    if running:
        return public(running)
    raise JobError(409, "This video has already finished.")


async def worker_status() -> dict:
    """For Ops: is the switch on, has a worker asked for work lately, and what is waiting."""
    state = await _state().find_one({"_id": "render_worker"}) or {}
    seen = _aware(state.get("last_seen"))
    now = _now()
    counts = {s: await _jobs().count_documents({"status": s}) for s in ("queued", "claimed", "done", "failed", "blocked", "cancelled")}
    oldest = await _jobs().find_one({"status": "queued"}, sort=[("created_at", 1)])
    waited = _aware(oldest["created_at"]) if oldest else None
    return {
        "enabled": bool(settings.VIDEO_WORKER_ENABLED),
        "token_set": bool(settings.VIDEO_WORKER_TOKEN),
        "online": bool(seen and now - seen < WORKER_OFFLINE_AFTER),
        "last_seen": seen.isoformat().replace("+00:00", "Z") if seen else None,
        "worker_id": state.get("worker_id"), "version": state.get("version"), "capabilities": state.get("capabilities"),
        "jobs": counts, "oldest_waiting_seconds": int((now - waited).total_seconds()) if waited else None,
        "lease_seconds": lease_seconds(), "max_attempts": settings.VIDEO_WORKER_MAX_ATTEMPTS,
        "max_output_seconds": settings.VIDEO_MAX_OUTPUT_SECONDS,
    }
