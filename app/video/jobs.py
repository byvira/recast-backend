"""Render jobs for the video worker.

A job is one row in `video_jobs`. The backend owns the truth: a worker claims a job under a lease, keeps it alive with
heartbeats and reports the result. A worker that goes quiet loses the job to the queue. Nothing finished is shown to a member
until it has passed every check in `complete` (the quality report, size, storage checksum and the Content Guard on frames).

States: queued, claimed, done, failed, cancelled, blocked (the Content Guard held the video back).
"""
from __future__ import annotations

import hashlib
import json
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
from app.video import runs, storage_params

logger = logging.getLogger(__name__)

CONTRACT = 1
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
INPUT_KINDS = ("video", "audio", "image")
OUTPUT_KEYS = ("filename", "segment_seconds", "crf", "preset", "audio_bitrate", "lufs", "label")
#: Written into the finished file when AI-made material went into the video.
AI_FILE_LABEL = "Includes AI-made material"
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

def quality_summary(report: Any, target_lufs: Optional[float] = None) -> Optional[dict]:
    """What was measured on a finished video, for the member: the numbers that mean something, and plain notes for what is worth a look.
    The worker's own warnings are for staff and are not passed on as they are."""
    if not isinstance(report, dict):
        return None

    def num(key: str) -> Optional[float]:
        v = report.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    black, frozen, loud = num("black_seconds"), num("frozen_seconds"), num("loudness_lufs")
    notes: list[str] = []
    if black is not None and black >= 0.5:
        notes.append(f"About {black:.1f} seconds of the picture are black.")
    if frozen is not None and frozen >= 3:
        notes.append(f"About {frozen:.1f} seconds of the picture do not move.")
    if any("silent" in str(w).lower() for w in report.get("warnings") or []):
        notes.append("The sound track is silent.")
    if loud is not None and target_lufs is not None and abs(loud - target_lufs) > 2.5:
        notes.append(f"The loudness came out at {loud:.1f}, a little away from the {target_lufs:g} you chose.")
    return {
        "passed": report.get("passed") is True, "duration_s": num("duration_s"), "width": report.get("width"), "height": report.get("height"),
        "fps": num("fps"), "loudness_lufs": loud, "true_peak_dbfs": num("true_peak_dbfs"), "black_seconds": black, "frozen_seconds": frozen,
        "notes": notes,
    }


def public(doc: Optional[dict]) -> Optional[dict]:
    """The job as the API returns it: no lease secret, no source addresses, no edit list."""
    if not doc:
        return None
    out = {k: doc.get(k) for k in (
        "id", "workspace_id", "label", "status", "attempts", "duration_s", "media_id", "error", "error_code",
        "progress", "cancel_requested", "created_at", "claimed_at", "finished_at", "origin", "project_id", "run_id", "kind",
    )}
    for field in ("created_at", "claimed_at", "finished_at"):
        value = _aware(out.get(field))
        out[field] = value.isoformat().replace("+00:00", "Z") if value else None
    if doc.get("output"):
        out["output"] = {k: doc["output"].get(k) for k in ("url", "bytes", "duration_s", "width", "height")}
    if doc.get("status") == "done" and doc.get("report"):
        out["quality"] = quality_summary(doc.get("report"), doc.get("target_lufs"))
    if doc.get("target_lufs") is not None:
        out["target_lufs"] = doc["target_lufs"]
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


MAX_REFRAME_LOOKS = 1500
MAX_REFRAME_POINTS = 240
HALT_MESSAGE = "Generation is paused for this workspace. An owner armed the emergency kill switch in Odette."
#: Jobs that only prepare what a member already uploaded (a small playable copy, sample pictures). They spend nothing, so the
#: kill switch does not hold them.
_PREPARE_KINDS = ("proxy", "frames", "reframe")


async def _halted(workspace_id: str) -> bool:
    ws = await get_db()["workspaces"].find_one({"id": workspace_id}, {"generation_halted": 1})
    return bool(ws and ws.get("generation_halted"))


async def _assert_video_on(workspace_id: str) -> None:
    from app.video import governance

    if not await governance.video_enabled(workspace_id):
        raise JobError(403, governance.ACCESS_OFF)


async def _halted_workspaces() -> list[str]:
    return [w["id"] async for w in get_db()["workspaces"].find({"generation_halted": True}, {"id": 1})]


async def enqueue(
    *, workspace_id: str, user_id: str, inputs: dict, edit: dict, output: Optional[dict] = None,
    label: str = "", idem_key: Optional[str] = None, origin: Optional[dict] = None, project_id: Optional[str] = None,
    href: Optional[str] = None, show_in_activity: bool = False,
) -> dict:
    """Queue one render. The same idempotency key in the same workspace returns the job already made. With
    `show_in_activity` the render also appears in the activity dock, Control Tower and the Activity Log as a run."""
    seconds = _validate_request(inputs, edit)
    await _assert_video_on(workspace_id)
    if await _halted(workspace_id):
        raise JobError(403, HALT_MESSAGE)
    if project_id:
        from app.video import limits

        existing_job = await _jobs().find_one({"workspace_id": workspace_id, "idem_key": idem_key}) if idem_key else None
        if not existing_job:
            try:
                await limits.assert_render_room(workspace_id, seconds)
            except limits.OverLimit as exc:
                raise JobError(429, exc.message)
    if idem_key:
        existing = await _jobs().find_one({"workspace_id": workspace_id, "idem_key": idem_key})
        if existing:
            return public(existing)
    now = _now()
    doc = {
        "id": str(uuid4()), "workspace_id": workspace_id, "user_id": user_id, "label": (label or "")[:120],
        "status": "queued", "attempts": 0, "inputs": inputs, "edit": edit,
        "output": {**{k: v for k, v in (output or {}).items() if k in OUTPUT_KEYS}, **({"label": AI_FILE_LABEL} if (origin or {}).get("contains_ai") else {})},
        "duration_s": round(seconds, 3), "created_at": now, "lease_token": None, "lease_expires_at": None,
        "cancel_requested": False, "progress": {}, "origin": origin or {"origin": "own", "contains_ai": False, "contains_stock": False},
    }
    if project_id:
        doc["project_id"] = project_id
    if (output or {}).get("lufs") is not None:
        doc["target_lufs"] = output["lufs"]
    if idem_key:
        doc["idem_key"] = idem_key
    if show_in_activity:
        run = await runs.open_run(
            workspace_id=workspace_id, user_id=user_id, title=label, project_id=project_id, job_id=doc["id"], href=href,
        )
        doc["run_id"] = run["id"]
    try:
        await _jobs().insert_one(dict(doc))
    except DuplicateKeyError:
        await runs.finish_cancelled(doc.get("run_id"))
        return public(await _jobs().find_one({"workspace_id": workspace_id, "idem_key": idem_key}))
    return public(doc)


PREVIEW_ROLES = ("proxy", "poster", "strip", "waveform")


async def enqueue_preview(*, workspace_id: str, user_id: str, media_id: str, url: str, kind: str, spec: Optional[dict] = None) -> dict:
    """Queue the preview material for one library item: a small playable copy, a poster, a strip of thumbnails and waveform
    peaks. One job per item; asking again while it is waiting or after it is done returns the same job."""
    if not str(url).startswith("https://") or kind not in INPUT_KINDS:
        raise JobError(400, "That item cannot be previewed.")
    existing = await _jobs().find_one({"workspace_id": workspace_id, "idem_key": f"preview:{media_id}"})
    if existing:
        return public(existing)
    now = _now()
    doc = {
        "id": str(uuid4()), "workspace_id": workspace_id, "user_id": user_id, "label": "Preview", "kind": "proxy",
        "status": "queued", "attempts": 0, "inputs": {"src": {"url": url, "kind": kind}}, "proxy": spec or {},
        "source_media_id": media_id, "duration_s": 0, "created_at": now, "lease_token": None, "lease_expires_at": None,
        "cancel_requested": False, "progress": {}, "idem_key": f"preview:{media_id}",
    }
    try:
        await _jobs().insert_one(dict(doc))
    except DuplicateKeyError:
        return public(await _jobs().find_one({"workspace_id": workspace_id, "idem_key": f"preview:{media_id}"}))
    return public(doc)


MAX_COVER_FRAMES = 48
MAX_SPEECH_LINES = 80


async def enqueue_speech(*, workspace_id: str, user_id: str, items: list[dict], language: str, voice: Optional[str] = None, speed: float = 1.0) -> dict:
    """Queue spoken lines to be made with the natural voice on the worker. `items` are {"id", "text"}; each comes back as a small
    audio file. Only a worker that reports a voice is given this job."""
    clean = []
    for it in items[:MAX_SPEECH_LINES]:
        text = str(it.get("text") or "").strip()[:1200]
        if text and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(it.get("id", ""))):
            clean.append({"id": str(it["id"]), "text": text})
    if not clean:
        raise JobError(400, "There is nothing to speak.")
    await _assert_video_on(workspace_id)
    if await _halted(workspace_id):
        raise JobError(403, HALT_MESSAGE)
    now = _now()
    doc = {
        "id": str(uuid4()), "workspace_id": workspace_id, "user_id": user_id, "label": "Voice", "kind": "speech", "status": "queued", "attempts": 0,
        "inputs": {}, "speech": {"language": language.lower()[:8], "items": clean, **({"voice": voice} if voice else {}), "speed": speed},
        "duration_s": 0, "created_at": now, "lease_token": None, "lease_expires_at": None, "cancel_requested": False, "progress": {},
    }
    await _jobs().insert_one(dict(doc))
    return public(doc)


async def enqueue_frames(*, workspace_id: str, user_id: str, media_id: str, url: str, times_s: list[float], width: int = 1280) -> dict:
    """Queue the candidate cover frames of one video: a picture at each chosen moment with its measurements. The same moments of
    the same video return the job that exists; a failed or cancelled one is replaced."""
    if not str(url).startswith("https://"):
        raise JobError(400, "That video cannot be looked at.")
    times = sorted({round(float(t), 2) for t in times_s if isinstance(t, (int, float)) and not isinstance(t, bool) and t >= 0})[:MAX_COVER_FRAMES]
    if not times:
        raise JobError(400, "There are no moments to take pictures of.")
    key = f"frames:{media_id}:{hashlib.sha1(json.dumps([times, width]).encode()).hexdigest()[:16]}"
    existing = await _jobs().find_one({"workspace_id": workspace_id, "idem_key": key})
    if existing and existing.get("status") in ("failed", "cancelled", "blocked"):
        await _jobs().update_one({"id": existing["id"]}, {"$set": {"idem_key": f"{key}:old:{existing['id']}"}})
        existing = None
    if existing:
        return public(existing)
    now = _now()
    doc = {
        "id": str(uuid4()), "workspace_id": workspace_id, "user_id": user_id, "label": "Cover pictures", "kind": "frames",
        "status": "queued", "attempts": 0, "inputs": {"src": {"url": url, "kind": "video"}}, "frames": {"times_s": times, "width": width},
        "source_media_id": media_id, "duration_s": 0, "created_at": now, "lease_token": None, "lease_expires_at": None,
        "cancel_requested": False, "progress": {}, "idem_key": key,
    }
    try:
        await _jobs().insert_one(dict(doc))
    except DuplicateKeyError:
        return public(await _jobs().find_one({"workspace_id": workspace_id, "idem_key": key}))
    return public(doc)


async def enqueue_reframe(*, workspace_id: str, user_id: str, media_id: str, url: str, aspect: float, start_s: float, end_s: float, step_s: float = 0.4) -> dict:
    """Queue the follow-the-speaker pass over one part of a video. The same part at the same shape returns the job that exists."""
    if not str(url).startswith("https://"):
        raise JobError(400, "That video cannot be looked at.")
    spec = {"aspect": round(float(aspect), 4), "start_s": round(float(start_s), 2), "end_s": round(float(end_s), 2), "step_s": round(float(step_s), 2)}
    if not (0.2 <= spec["aspect"] <= 5.0) or spec["end_s"] <= spec["start_s"] or not (0.2 <= spec["step_s"] <= 2.0):
        raise JobError(400, "That part of the video cannot be looked at.")
    if (spec["end_s"] - spec["start_s"]) / spec["step_s"] > MAX_REFRAME_LOOKS:
        raise JobError(400, "That part is too long to follow in one go. Pick a shorter part.")
    key = f"reframe:{media_id}:{hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]}"
    existing = await _jobs().find_one({"workspace_id": workspace_id, "idem_key": key})
    if existing and existing.get("status") in ("failed", "cancelled", "blocked"):
        await _jobs().update_one({"id": existing["id"]}, {"$set": {"idem_key": f"{key}:old:{existing['id']}"}})
        existing = None
    if existing:
        return public(existing)
    doc = {
        "id": str(uuid4()), "workspace_id": workspace_id, "user_id": user_id, "label": "Following the speaker", "kind": "reframe",
        "status": "queued", "attempts": 0, "inputs": {"src": {"url": url, "kind": "video"}}, "reframe": spec, "source_media_id": media_id,
        "duration_s": 0, "created_at": _now(), "lease_token": None, "lease_expires_at": None, "cancel_requested": False, "progress": {}, "idem_key": key,
    }
    try:
        await _jobs().insert_one(dict(doc))
    except DuplicateKeyError:
        return public(await _jobs().find_one({"workspace_id": workspace_id, "idem_key": key}))
    return public(doc)


# ── the worker's side ────────────────────────────────────────────────────────────

async def touch_worker(worker_id: str, version: str, capabilities: dict) -> None:
    await _state().update_one(
        {"_id": "render_worker"},
        {"$set": {"worker_id": worker_id[:80], "version": version[:40], "capabilities": capabilities, "last_seen": _now()}},
        upsert=True,
    )


QUEUE_EXPIRES_AFTER = timedelta(hours=24)


async def _close_job(doc: dict, status: str, *, code: Optional[str] = None, message: Optional[str] = None) -> bool:
    """End a job that is not finished and tell its run. True when this call is the one that closed it."""
    now = _now()
    changed = await _jobs().update_one(
        {"id": doc["id"], "status": {"$in": ["queued", "claimed"]}},
        {"$set": {"status": status, "error_code": code, "error": message, "finished_at": now, "lease_token": None, "lease_expires_at": None}},
    )
    if not changed.modified_count:
        return False
    if status == "cancelled":
        await runs.finish_cancelled(doc.get("run_id"))
    else:
        await runs.finish_failed(doc.get("run_id"), message or "The video could not be made.")
    if doc.get("kind") in (None, "render"):
        from app.video import activity

        await activity.render_ended(doc["workspace_id"], doc.get("user_id"), doc.get("project_id"), doc["id"], status, message or "")
    return True


async def reap_expired() -> None:
    """The queue's reconciler. A job whose worker stopped reporting goes back in the queue, or is failed when it is out of
    attempts; one the member cancelled is closed; one waiting for a worker for a day is failed with a plain message; and a
    queued job whose run was cancelled from the activity dock is cancelled."""
    now = _now()
    again = "The worker went quiet, so this is starting again."
    async for doc in _jobs().find({"status": "claimed", "lease_expires_at": {"$lt": now}}).limit(200):
        if doc.get("cancel_requested"):
            await _close_job(doc, "cancelled")
        elif doc["attempts"] >= settings.VIDEO_WORKER_MAX_ATTEMPTS:
            await _close_job(doc, "failed", code="worker_lost",
                             message="The video could not be finished after several tries. Please try again.")
        else:
            moved = await _jobs().update_one(
                {"id": doc["id"], "status": "claimed", "lease_expires_at": {"$lt": now}},
                {"$set": {"status": "queued", "lease_token": None, "lease_expires_at": None, "progress": {}}},
            )
            if moved.modified_count:
                await runs.waiting(doc.get("run_id"), again)
    async for doc in _jobs().find({"status": "queued", "created_at": {"$lt": now - QUEUE_EXPIRES_AFTER}}).limit(100):
        await _close_job(doc, "failed", code="no_worker",
                         message="No video worker was available for a day, so this was stopped. Please try again.")
    async for doc in _jobs().find({"status": "queued", "run_id": {"$ne": None}}).limit(200):
        if await runs.cancel_requested(doc.get("run_id")):
            await _close_job(doc, "cancelled")


def _wire(doc: dict) -> dict:
    if doc.get("kind") == "speech":
        return {"id": doc["id"], "kind": "speech", "contract": CONTRACT, "lease_token": doc["lease_token"], "lease_seconds": lease_seconds(),
                "inputs": {}, "speech": doc.get("speech") or {}, "limits": {}}
    if doc.get("kind") == "reframe":
        return {"id": doc["id"], "kind": "reframe", "contract": CONTRACT, "lease_token": doc["lease_token"],
                "lease_seconds": lease_seconds(), "inputs": doc["inputs"], "reframe": doc.get("reframe") or {}, "limits": {}}
    if doc.get("kind") == "frames":
        return {"id": doc["id"], "kind": "frames", "contract": CONTRACT, "lease_token": doc["lease_token"],
                "lease_seconds": lease_seconds(), "inputs": doc["inputs"], "frames": doc.get("frames") or {}, "limits": {}}
    if doc.get("kind") == "proxy":
        return {"id": doc["id"], "kind": "proxy", "contract": CONTRACT, "lease_token": doc["lease_token"],
                "lease_seconds": lease_seconds(), "inputs": doc["inputs"], "proxy": doc.get("proxy") or {}, "limits": {}}
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
    base: dict[str, Any] = {"status": "queued", "duration_s": {"$lte": cap}, "cancel_requested": {"$ne": True}}
    if not capabilities.get("speech"):
        base["kind"] = {"$ne": "speech"}                 # a worker without a voice is never handed spoken lines
    halted = await _halted_workspaces()
    if halted:
        # the owner's kill switch holds queued renders and voices; they run when it is released
        base["$or"] = [{"workspace_id": {"$nin": halted}}, {"kind": {"$in": list(_PREPARE_KINDS)}}]
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
            await runs.started(doc.get("run_id"))
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
    shown = doc.get("progress") or {}
    cancel = bool(doc.get("cancel_requested"))
    if doc.get("run_id"):
        cancel = await runs.progress(doc["run_id"], shown.get("stage", ""), shown.get("fraction")) or cancel
    if cancel:
        # The worker stops without reporting when it is told to cancel, so the job is closed here, at the moment it is told.
        await _jobs().update_one({"id": job_id}, {"$set": {"cancel_requested": True}})
        await _close_job(doc, "cancelled")
    return {"cancel": cancel, "lease_seconds": lease_seconds()}


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
    if doc.get("kind") in (None, "render"):
        from app.video import activity

        await activity.render_ended(doc["workspace_id"], doc.get("user_id"), doc.get("project_id"), doc["id"], status, message)


async def complete(job_id: str, lease_token: str, outputs: Any, report: Any) -> dict:
    """Accept a finished render. The video is shown to nobody until the report, the size, the stored file and the sample
    frames all check out; anything missing ends the job with a plain message and keeps the evidence for staff."""
    doc = await _held(job_id, lease_token)
    if doc.get("kind") == "proxy":
        return await _complete_preview(doc, job_id, lease_token, outputs, report)
    if doc.get("kind") == "reframe":
        return await _complete_reframe(doc, job_id, lease_token, report)
    if doc.get("kind") == "frames":
        return await _complete_frames(doc, job_id, lease_token, outputs, report)
    if doc.get("kind") == "speech":
        return await _complete_speech(doc, job_id, lease_token, outputs, report)
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
        message = "The video was made but could not be checked, so it was not saved. Please try again."
        await _end(doc, "failed", "delivery_mismatch", message, {"problem": problem, "report": report})
        await runs.finish_failed(doc.get("run_id"), message)
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
            await runs.finish_failed(doc.get("run_id"), message)
            return {"ok": True, "accepted": False, "held": True}
    await _forget_files([f["url"] for f in frames if storage_params.is_our_storage_url(f["url"])])

    now = _now()
    media = MediaAsset(
        id=str(uuid4()), workspace_id=doc["workspace_id"], kind=MediaKind.VIDEO, url=video["url"], mime_type="video/mp4",
        width=report.get("width"), height=report.get("height"), duration_s=round(float(report["duration_s"]), 2),
        source=MediaSource.ENHANCED, created_by=doc["user_id"], created_at=now, size_bytes=video["bytes"],
        **{k: (doc.get("origin") or {}).get(k) for k in ("origin", "contains_ai", "contains_stock")},
    )
    await media_assets.insert_one(media.model_dump())
    await _jobs().update_one(
        {"id": job_id, "lease_token": lease_token},
        {"$set": {"status": "done", "media_id": media.id, "finished_at": now, "lease_token": None, "lease_expires_at": None,
                  "output": {"url": video["url"], "bytes": video["bytes"], "sha256": video.get("sha256"),
                             "duration_s": report.get("duration_s"), "width": report.get("width"), "height": report.get("height")},
                  "report": report, "progress": {"stage": "Done", "fraction": 1.0}}},
    )
    await runs.finish_ok(doc.get("run_id"), {
        "job_id": job_id, "media_id": media.id, "project_id": doc.get("project_id"), "origin": doc.get("origin"),
        "duration_s": report.get("duration_s"),
    })
    from app.video import activity

    await activity.video_made(
        doc["workspace_id"], doc.get("user_id"), doc.get("project_id"), job_id, duration_s=report.get("duration_s"),
        origin=(doc.get("origin") or {}).get("origin"),
    )
    return {"ok": True, "accepted": True, "media_id": media.id}


async def _complete_preview(doc: dict, job_id: str, lease_token: str, outputs: Any, report: Any) -> dict:
    """Accept preview material. Every file must be in our storage and be the size reported; the poster goes through the Content
    Guard like any picture. A refusal ends the job and removes what was uploaded, and leaves the member's original untouched."""
    from app.agents.content_guard.media import ContentRejected, assert_image_ok

    media_id = doc.get("source_media_id")
    items = [o for o in (outputs if isinstance(outputs, list) else []) if isinstance(o, dict) and o.get("role") in PREVIEW_ROLES]
    stored = [o["url"] for o in items if isinstance(o.get("url"), str) and storage_params.is_our_storage_url(o["url"])]

    async def refuse(why: str) -> dict:
        await _forget_files(stored)
        message = "The preview could not be made, so this video can't be edited yet. Please try again."
        await _end(doc, "failed", "preview_failed", message, {"problem": why})
        await media_assets.update_one({"id": media_id, "workspace_id": doc["workspace_id"]},
                                      {"$set": {"preview_status": "failed", "preview_error": message}})
        logger.warning("Preview %s refused: %s", job_id, why)
        return {"ok": False, "accepted": False}

    if not isinstance(report, dict) or report.get("passed") is not True or report.get("kind") != "proxy":
        return await refuse("the report does not say the preview passed")
    if not items or any(not isinstance(o.get("bytes"), int) or o["bytes"] <= 0 for o in items):
        return await refuse("a preview file is missing or empty")
    if len(stored) != len(items):
        return await refuse("a preview file is not in our storage")
    for o in items:
        size = await _remote_size(o["url"])
        if size is None or size != o["bytes"]:
            return await refuse(f"the stored {o['role']} is not the size that was reported")

    poster = next((o for o in items if o["role"] == "poster"), None)
    if poster:
        data = await _fetch_frame(poster["url"])
        if data:
            try:
                await assert_image_ok(data, "image/jpeg", workspace_id=doc["workspace_id"], where="video preview")
            except ContentRejected as exc:
                await _forget_files(stored)
                message = exc.detail.get("message") if isinstance(exc.detail, dict) else "This video was held back by the safety check."
                await _end(doc, "blocked", "content_guard", message, {"categories": (exc.detail or {}).get("categories")})
                await media_assets.update_one(
                    {"id": media_id, "workspace_id": doc["workspace_id"]},
                    {"$set": {"preview_status": "blocked", "safety_hold": {"message": message, "at": _now()}}},
                )
                return {"ok": True, "accepted": False, "held": True}

    by_role = {o["role"]: o for o in items}
    preview = {
        **{f"{role}_url": by_role[role]["url"] for role in by_role},
        **{f"{role}_bytes": by_role[role]["bytes"] for role in by_role},
        "meta": {k: v for k, v in report.items() if k in ("duration_s", "has_video", "has_audio", "is_image", "width", "height", "fps",
                                                          "proxy", "strip", "waveform", "scenes")},
        "generated_at": _now(),
    }
    fields: dict[str, Any] = {"preview": preview, "preview_status": "ready"}
    if poster:
        fields["poster_url"] = poster["url"]
    await media_assets.update_one({"id": media_id, "workspace_id": doc["workspace_id"]}, {"$set": fields})
    await _jobs().update_one(
        {"id": job_id, "lease_token": lease_token},
        {"$set": {"status": "done", "media_id": media_id, "finished_at": _now(), "lease_token": None, "lease_expires_at": None,
                  "progress": {"stage": "Done", "fraction": 1.0}, "report": report}},
    )
    return {"ok": True, "accepted": True, "media_id": media_id}


async def _complete_speech(doc: dict, job_id: str, lease_token: str, outputs: Any, report: Any) -> dict:
    """Accept spoken lines. Every file must be in our storage at the size reported and every line asked for must be there; a report
    that does not say so ends the job, and the voice falls back to the draft voice."""
    items = [o for o in (outputs if isinstance(outputs, list) else []) if isinstance(o, dict) and o.get("role") == "speech"]
    stored = [o["url"] for o in items if isinstance(o.get("url"), str) and storage_params.is_our_storage_url(o["url"])]

    async def refuse(why: str) -> dict:
        await _forget_files(stored)
        message = "The voice could not be made. Please try again."
        await _end(doc, "failed", "speech_failed", message, {"problem": why})
        await runs.finish_failed(doc.get("run_id"), message)
        logger.warning("Speech %s refused: %s", job_id, why)
        return {"ok": False, "accepted": False}

    if not isinstance(report, dict) or report.get("passed") is not True or report.get("kind") != "speech":
        return await refuse("the report does not say the speech passed")
    rows = report.get("items")
    wanted = {it["id"] for it in (doc.get("speech") or {}).get("items", [])}
    if not items or not isinstance(rows, list) or len(items) != len(stored):
        return await refuse("a voice file is missing or not in our storage")
    by_name = {o.get("name"): o for o in items}
    made: list[dict[str, Any]] = []
    for r in rows:
        o = by_name.get(r.get("file")) if isinstance(r, dict) else None
        sec = r.get("seconds") if isinstance(r, dict) else None
        if o is None or r.get("id") not in wanted or not isinstance(o.get("bytes"), int) or o["bytes"] <= 0 \
                or not isinstance(sec, (int, float)) or isinstance(sec, bool) or not (0 < sec <= 600):
            return await refuse("a spoken line has no matching file or length")
        size = await _remote_size(o["url"])
        if size is None or size != o["bytes"]:
            return await refuse("a stored voice file is not the size that was reported")
        made.append({"id": r["id"], "url": o["url"], "bytes": o["bytes"], "seconds": round(float(sec), 2)})
    if {m["id"] for m in made} != wanted:
        return await refuse("some lines were not spoken")
    now = _now()
    await get_db()["video_speech"].update_one(
        {"job_id": job_id}, {"$set": {"job_id": job_id, "workspace_id": doc["workspace_id"], "items": made, "language": report.get("language"),
                                      "created_at": now}}, upsert=True)
    await _jobs().update_one(
        {"id": job_id, "lease_token": lease_token},
        {"$set": {"status": "done", "finished_at": now, "lease_token": None, "lease_expires_at": None,
                  "progress": {"stage": "Done", "fraction": 1.0}, "report": {k: v for k, v in report.items() if k != "items"}}},
    )
    await runs.finish_ok(doc.get("run_id"), {"job_id": job_id, "lines": len(made)})
    return {"ok": True, "accepted": True}


def _clean_path(raw: Any, length_s: float) -> Optional[list[dict[str, float]]]:
    """The path a worker reported, kept only if every point is a plain number inside the picture and the times go forward."""
    if not isinstance(raw, list) or len(raw) > MAX_REFRAME_POINTS:
        return None
    out: list[dict[str, float]] = []
    last = -1.0
    for p in raw:
        if not isinstance(p, dict):
            return None
        vals = [p.get(k) for k in ("t", "x", "y")]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            return None
        t, x, y = (float(v) for v in vals)
        if not (0 <= x <= 1 and 0 <= y <= 1 and 0 <= t <= length_s + 1 and t > last):
            return None
        last = t
        out.append({"t": round(t, 3), "x": round(x, 4), "y": round(y, 4)})
    return out


async def _complete_reframe(doc: dict, job_id: str, lease_token: str, report: Any) -> dict:
    """Accept a follow-the-speaker result: a path of window positions, or an honest note that no face finder was available."""
    media_id = doc.get("source_media_id")
    spec = doc.get("reframe") or {}

    async def refuse(why: str) -> dict:
        message = "The speaker could not be followed. Please try again."
        await _end(doc, "failed", "reframe_failed", message, {"problem": why})
        await runs.finish_failed(doc.get("run_id"), message)
        logger.warning("Reframe %s refused: %s", job_id, why)
        return {"ok": False, "accepted": False}

    if not isinstance(report, dict) or report.get("passed") is not True or report.get("kind") != "reframe":
        return await refuse("the report does not say the pass finished")
    path = _clean_path(report.get("path"), float(spec.get("end_s", 0)) - float(spec.get("start_s", 0)))
    window = report.get("window") or {}
    if path is None or not all(isinstance(window.get(k), (int, float)) and 0 < window[k] <= 1 for k in ("w", "h")):
        return await refuse("the path or the window is not valid")
    checked = bool(report.get("faces_checked"))
    now = _now()
    await get_db()["video_reframe_paths"].update_one(
        {"workspace_id": doc["workspace_id"], "media_id": media_id, "key": doc["idem_key"]},
        {"$set": {"workspace_id": doc["workspace_id"], "media_id": media_id, "key": doc["idem_key"], "job_id": job_id, "spec": spec, "path": path,
                  "window": {"w": float(window["w"]), "h": float(window["h"])}, "faces_checked": checked,
                  "looked": int(report.get("looked") or 0), "with_face": int(report.get("with_face") or 0), "created_at": now}},
        upsert=True,
    )
    await _jobs().update_one(
        {"id": job_id, "lease_token": lease_token},
        {"$set": {"status": "done", "media_id": media_id, "finished_at": now, "lease_token": None, "lease_expires_at": None,
                  "progress": {"stage": "Done", "fraction": 1.0}, "report": {k: v for k, v in report.items() if k != "path"}}},
    )
    await runs.finish_ok(doc.get("run_id"), {"job_id": job_id, "media_id": media_id, "points": len(path)})
    return {"ok": True, "accepted": True, "media_id": media_id}


_FRAME_KEYS = ("brightness", "contrast", "sharpness", "top_busy", "bottom_busy", "hash", "rgb", "faces")


async def _complete_frames(doc: dict, job_id: str, lease_token: str, outputs: Any, report: Any) -> dict:
    """Accept candidate cover frames. Each picture must be in our storage and the size reported, and every measurement is checked
    to be a plain number before it is kept. The pictures stay in storage with their measurements until the member's covers are
    made from them."""
    media_id = doc.get("source_media_id")
    items = [o for o in (outputs if isinstance(outputs, list) else []) if isinstance(o, dict) and o.get("role") == "frame"]
    stored = [o["url"] for o in items if isinstance(o.get("url"), str) and storage_params.is_our_storage_url(o["url"])]

    async def refuse(why: str) -> dict:
        await _forget_files(stored)
        message = "The cover pictures could not be made. Please try again."
        await _end(doc, "failed", "frames_failed", message, {"problem": why})
        await runs.finish_failed(doc.get("run_id"), message)
        logger.warning("Frames %s refused: %s", job_id, why)
        return {"ok": False, "accepted": False}

    if not isinstance(report, dict) or report.get("passed") is not True or report.get("kind") != "frames":
        return await refuse("the report does not say the frames passed")
    rows = report.get("frames")
    if not items or not isinstance(rows, list) or len(items) > MAX_COVER_FRAMES:
        return await refuse("the pictures are missing")
    if len(stored) != len(items):
        return await refuse("a picture is not in our storage")
    by_name = {o.get("name"): o for o in items}
    frames: list[dict[str, Any]] = []
    for r in rows[:MAX_COVER_FRAMES]:
        o = by_name.get(r.get("file")) if isinstance(r, dict) else None
        if o is None or not isinstance(o.get("bytes"), int) or o["bytes"] <= 0:
            return await refuse("a picture has no matching file")
        size = await _remote_size(o["url"])
        if size is None or size != o["bytes"]:
            return await refuse("a stored picture is not the size that was reported")
        t = r.get("t")
        if not isinstance(t, (int, float)) or isinstance(t, bool) or t < 0:
            return await refuse("a picture has no time")
        item: dict[str, Any] = {"id": f"f{len(frames) + 1}", "t": round(float(t), 2), "url": o["url"], "bytes": o["bytes"]}
        for k in _FRAME_KEYS:
            v = r.get(k)
            if k == "hash":
                if isinstance(v, str) and len(v) == 16 and all(c in "0123456789abcdef" for c in v):
                    item[k] = v
            elif k == "rgb":
                if isinstance(v, list) and len(v) == 3 and all(isinstance(c, int) and 0 <= c <= 255 for c in v):
                    item[k] = v
            elif k == "faces":
                if isinstance(v, list):
                    item[k] = [
                        {a: round(float(f[a]), 4) for a in ("x", "y", "w", "h")} for f in v[:6]
                        if isinstance(f, dict) and all(isinstance(f.get(a), (int, float)) and 0 <= f[a] <= 1 for a in ("x", "y", "w", "h"))
                    ]
                else:
                    item[k] = None
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1:
                item[k] = round(float(v), 4)
        if "sharpness" not in item or "brightness" not in item:
            return await refuse("a picture has no measurements")
        frames.append(item)
    now = _now()
    await get_db()["video_cover_frames"].update_one(
        {"workspace_id": doc["workspace_id"], "media_id": media_id},
        {"$set": {"workspace_id": doc["workspace_id"], "media_id": media_id, "job_id": job_id, "frames": frames,
                  "faces_checked": bool(report.get("faces_checked")), "width": report.get("width"), "height": report.get("height"),
                  "created_at": now}},
        upsert=True,
    )
    await _jobs().update_one(
        {"id": job_id, "lease_token": lease_token},
        {"$set": {"status": "done", "media_id": media_id, "finished_at": now, "lease_token": None, "lease_expires_at": None,
                  "progress": {"stage": "Done", "fraction": 1.0}, "report": {k: v for k, v in report.items() if k != "frames"}}},
    )
    await runs.finish_ok(doc.get("run_id"), {"job_id": job_id, "media_id": media_id, "frames": len(frames)})
    return {"ok": True, "accepted": True, "media_id": media_id}


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
        await runs.waiting(doc.get("run_id"), "Starting again")
        return {"ok": True, "requeued": True}
    await _end(doc, "failed", code, safe, detail)
    await runs.finish_failed(doc.get("run_id"), safe)
    if doc.get("kind") == "proxy" and doc.get("source_media_id"):
        await media_assets.update_one({"id": doc["source_media_id"], "workspace_id": doc["workspace_id"]},
                                      {"$set": {"preview_status": "failed", "preview_error": safe}})
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
        await runs.finish_cancelled(done.get("run_id"))
        return public(done)
    running = await _jobs().find_one_and_update(
        {"id": job_id, "workspace_id": workspace_id, "status": "claimed"},
        {"$set": {"cancel_requested": True}}, return_document=ReturnDocument.AFTER,
    )
    if running:
        return public(running)
    raise JobError(409, "This video has already finished.")


async def worker_ready() -> bool:
    """True when the render worker is switched on and has asked for work lately, so a render handed to it will be picked up."""
    if not settings.VIDEO_WORKER_ENABLED or not settings.VIDEO_WORKER_TOKEN:
        return False
    state = await _state().find_one({"_id": "render_worker"}) or {}
    seen = _aware(state.get("last_seen"))
    return bool(seen and _now() - seen < WORKER_OFFLINE_AFTER)


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
