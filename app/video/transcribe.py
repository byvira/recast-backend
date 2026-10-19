"""Transcription of video of any length.

The sound is cut into pieces of about twenty minutes that overlap by two seconds, each piece is transcribed on its own, and the
words are joined at the midpoint of each overlap, so no word is lost or said twice at a join. Every piece is a saved step:
a run that stops (a restart, the daily speech allowance running out) carries on from the next piece, and a piece that failed is
transcribed again alone. The speech service's free allowance is shared by the whole product, so it is counted here and a long
file is queued for the next hour or day instead of failing.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import imageio_ffmpeg

from app.core.config import settings
from app.db.mongo import get_db, media_assets

logger = logging.getLogger(__name__)

CHUNK_SECONDS = 1200.0
OVERLAP_SECONDS = 2.0
PAGE_WORDS = 2000
MAX_CHUNK_ATTEMPTS = 3
LEASE_SECONDS = 300
# Languages the faster, cheaper model handles well; the others use the large model first.
TURBO_FIRST = {"en", "english"}
_PROTOCOLS = "file,https,tcp,tls,crypto"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    return value.replace(tzinfo=timezone.utc) if isinstance(value, datetime) and value.tzinfo is None else value


def _transcripts():
    return get_db()["video_transcripts"]


def _chunks():
    return get_db()["video_transcript_chunks"]


def _pages():
    return get_db()["video_transcript_pages"]


# ── planning and merging (pure) ──────────────────────────────────────────────────────────────────────────────────────────

def plan_chunks(duration_s: float, chunk_s: Optional[float] = None, overlap_s: Optional[float] = None) -> list[dict[str, Any]]:
    """Pieces that cover the whole sound. Each piece after the first starts `overlap_s` early so a word on a join is whole in at
    least one of them. A short remainder is folded into the last piece instead of being sent on its own."""
    if duration_s <= 0:
        return []
    chunk_s = chunk_s or CHUNK_SECONDS
    overlap_s = OVERLAP_SECONDS if overlap_s is None else overlap_s
    spans: list[tuple[float, float]] = []
    start = 0.0
    while start < duration_s:
        end = min(duration_s, start + chunk_s)
        if duration_s - end < 30 and end < duration_s:
            end = duration_s
        spans.append((start, end))
        start = end
    out: list[dict[str, Any]] = []
    for index, (s, e) in enumerate(spans):
        begin = max(0.0, s - overlap_s) if index else 0.0
        out.append({"index": index, "start_s": round(begin, 3), "length_s": round(e - begin, 3), "owns_from_s": round(s, 3),
                    "owns_to_s": round(e, 3), "status": "pending", "attempts": 0, "words": 0, "error": None})
    return out


def merge_words(chunks: list[dict[str, Any]], pieces: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """The words of every piece in one list in absolute time. A piece owns the part of the timeline from its own start to the
    midpoint of the overlap with the next piece, so a word is taken from exactly one piece."""
    ordered = sorted(chunks, key=lambda c: c["index"])
    out: list[dict[str, Any]] = []
    for pos, ch in enumerate(ordered):
        words = pieces.get(ch["index"])
        if words is None:
            continue
        lo = ch["owns_from_s"] if pos == 0 else ch["owns_from_s"] - OVERLAP_SECONDS / 2
        hi = ch["owns_to_s"] - OVERLAP_SECONDS / 2 if pos < len(ordered) - 1 else float("inf")
        if pos == 0:
            lo = float("-inf")
        for w in words:
            start = ch["start_s"] + w["s_ms"] / 1000.0
            if lo <= start < hi:
                out.append({**w, "s_ms": int(round(start * 1000)), "e_ms": int(round((ch["start_s"] + w["e_ms"] / 1000.0) * 1000))})
    out.sort(key=lambda w: (w["s_ms"], w["e_ms"]))
    return out


# ── the speech allowance ─────────────────────────────────────────────────────────────────────────────────────────────────

async def reserve_seconds(seconds: float) -> tuple[bool, float]:
    """Count `seconds` of speech against this hour's and today's allowance. Returns (True, 0) when it fits, otherwise (False,
    seconds to wait). The allowance is for the whole product, so it is a shared counter."""
    now = _now()
    limits = (
        ("hour", now.strftime("%Y-%m-%dT%H"), float(settings.VIDEO_TRANSCRIBE_HOURLY_SECONDS), 3600 - (now.minute * 60 + now.second)),
        ("day", now.strftime("%Y-%m-%d"), float(settings.VIDEO_TRANSCRIBE_DAILY_SECONDS), 86400 - (now.hour * 3600 + now.minute * 60 + now.second)),
    )
    col = get_db()["video_transcribe_budget"]
    taken: list[str] = []
    for kind, key, cap, wait in limits:
        _id = f"{kind}:{key}"
        # a single piece larger than the whole window could never fit; let it through on an empty window
        room = max(0.0, cap - seconds)
        row = await col.find_one_and_update(
            {"_id": _id, "used": {"$lte": room}}, {"$inc": {"used": seconds}, "$set": {"at": now}}, upsert=False,
        )
        if row is None:
            try:
                await col.insert_one({"_id": _id, "used": seconds, "at": now})
            except Exception:  # noqa: BLE001 - it exists but is full, or another request just made it
                again = await col.find_one_and_update({"_id": _id, "used": {"$lte": room}}, {"$inc": {"used": seconds}})
                if again is None:
                    for done in taken:
                        await col.update_one({"_id": done}, {"$inc": {"used": -seconds}})
                    return False, float(wait) + 5.0
        taken.append(_id)
    return True, 0.0


async def budget_left() -> dict[str, float]:
    now = _now()
    col = get_db()["video_transcribe_budget"]
    hour = await col.find_one({"_id": f"hour:{now.strftime('%Y-%m-%dT%H')}"}) or {}
    day = await col.find_one({"_id": f"day:{now.strftime('%Y-%m-%d')}"}) or {}
    return {
        "hour_left_s": max(0.0, float(settings.VIDEO_TRANSCRIBE_HOURLY_SECONDS) - float(hour.get("used", 0))),
        "day_left_s": max(0.0, float(settings.VIDEO_TRANSCRIBE_DAILY_SECONDS) - float(day.get("used", 0))),
    }


# ── cutting the sound ────────────────────────────────────────────────────────────────────────────────────────────────────

class ChunkError(Exception):
    """A piece of sound could not be cut. The message is safe to show."""


async def cut_audio(source: str, start_s: float, length_s: float, *, timeout_s: float = 600.0) -> bytes:
    """One piece of the sound as a small speech-quality file (mono, 16 kHz, 32 kbps: about 5 MB for twenty minutes). Reads only
    the part it needs when the source is a link."""
    tmp = tempfile.mkdtemp(prefix="recast-vtx-")
    try:
        out = Path(tmp) / "piece.mp3"
        proc = await asyncio.create_subprocess_exec(
            imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-nostdin", "-v", "error", "-protocol_whitelist", _PROTOCOLS,
            "-ss", f"{start_s:.3f}", "-t", f"{length_s:.3f}", "-i", source, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(out),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()      # the stopped process is collected, not left behind
            raise ChunkError("Reading the sound of this video took too long. Please try again.")
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            logger.warning("Cutting audio failed (%s): %s", proc.returncode, (err or b"")[-300:].decode(errors="replace"))
            raise ChunkError("The sound of this video could not be read.")
        return out.read_bytes()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── the run ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _public(doc: Optional[dict]) -> Optional[dict]:
    if not doc:
        return None
    chunks = doc.get("chunks") or []
    done = sum(1 for c in chunks if c["status"] == "done")
    out = {k: doc.get(k) for k in ("id", "media_id", "status", "language", "duration_s", "word_count", "has_speech", "error", "speakers")}
    out["speaker_share"] = doc.get("speaker_share") or {}
    out["chunks_total"] = len(chunks)
    out["chunks_done"] = done
    out["chunks_failed"] = sum(1 for c in chunks if c["status"] == "failed" and c["attempts"] >= MAX_CHUNK_ATTEMPTS)
    out["progress"] = round(done / len(chunks), 3) if chunks else 0.0
    for f in ("waiting_until", "created_at", "updated_at", "finished_at"):
        v = _aware(doc.get(f))
        out[f] = v.isoformat().replace("+00:00", "Z") if v else None
    return out


public = _public


async def create(*, workspace_id: str, user_id: str, media_id: str, duration_s: float, language: Optional[str] = None,
                 speakers: bool = False) -> dict:
    """Start (or return) the transcript for a library video. One per item."""
    existing = await _transcripts().find_one({"workspace_id": workspace_id, "media_id": media_id})
    if existing:
        return existing
    now = _now()
    doc = {
        "id": str(uuid4()), "workspace_id": workspace_id, "media_id": media_id, "user_id": user_id, "status": "queued",
        "language": (language or "").strip().lower() or None, "language_locked": bool(language), "duration_s": float(duration_s),
        "chunks": plan_chunks(duration_s), "speakers": bool(speakers), "word_count": 0, "has_speech": None, "error": None,
        "waiting_until": None, "lease_until": None, "created_at": now, "updated_at": now, "finished_at": None,
    }
    try:
        await _transcripts().insert_one(dict(doc))
    except Exception:  # noqa: BLE001 - another request created it first
        return await _transcripts().find_one({"workspace_id": workspace_id, "media_id": media_id})
    return doc


async def _lease(transcript_id: str) -> Optional[dict]:
    now = _now()
    return await _transcripts().find_one_and_update(
        {"id": transcript_id, "status": {"$in": ["queued", "running", "waiting"]},
         "$or": [{"lease_until": None}, {"lease_until": {"$lt": now}}]},
        {"$set": {"status": "running", "lease_until": now + timedelta(seconds=LEASE_SECONDS), "updated_at": now}},
        return_document=True,
    )


async def _renew(transcript_id: str) -> None:
    await _transcripts().update_one({"id": transcript_id}, {"$set": {"lease_until": _now() + timedelta(seconds=LEASE_SECONDS), "updated_at": _now()}})


def _word_row(word: Any, speaker: Optional[str] = None) -> dict[str, Any]:
    row = {"w": word.word, "s_ms": int(round(word.start_s * 1000)), "e_ms": int(round(word.end_s * 1000))}
    sp = speaker if speaker is not None else getattr(word, "speaker", None)
    if sp is not None:
        row["sp"] = str(sp)
    return row


async def _transcribe_piece(audio: bytes, index: int, language: Optional[str], want_speakers: bool) -> tuple[list[dict], Optional[str], bool]:
    """(words, language, ok). `ok` is False when the provider failed, as opposed to a piece with no speech in it."""
    from app.pipelines.audio import deepgram_stt
    from app.pipelines.audio.transcriber import transcribe_audio_detailed

    if want_speakers and deepgram_stt.available():
        try:
            result = await deepgram_stt.listen(audio, f"piece{index}.mp3", language, utterances=False, diarize=True)
            rows = [
                {"w": w, "s_ms": int(round(s * 1000)), "e_ms": int(round(e * 1000)), **({"sp": str(sp)} if sp is not None else {})}
                for w, s, e, sp in deepgram_stt.speaker_words_from(result)
            ]
            return rows, deepgram_stt.detected_language(result) or language, True
        except Exception as exc:  # noqa: BLE001 - fall back to words without speakers
            logger.warning("Speaker labels failed for piece %s, using plain words: %s", index, exc)
    prefer_turbo = (language or "").lower() in TURBO_FIRST
    words, detected = await transcribe_audio_detailed(audio, f"piece{index}.mp3", language, True, prefer_turbo=prefer_turbo)
    if not words and detected is None:
        return [], language, False
    return [_word_row(w) for w in words], detected or language, True


async def run(transcript_id: str, *, reporter: Any = None, source: Optional[str] = None) -> dict:
    """Do the pieces that are not done yet. Returns the transcript's public state: done, waiting (the allowance is used up for
    now), or failed. Safe to call again; two callers never work on the same transcript at once."""
    doc = await _lease(transcript_id)
    if not doc:
        return _public(await _transcripts().find_one({"id": transcript_id})) or {}
    media = await media_assets.find_one({"id": doc["media_id"], "workspace_id": doc["workspace_id"]})
    src = source or (media or {}).get("url")
    if not src:
        await _transcripts().update_one({"id": transcript_id}, {"$set": {"status": "failed", "error": "This video is no longer in the library.", "lease_until": None}})
        return _public(await _transcripts().find_one({"id": transcript_id}))
    language = doc.get("language")
    locked = bool(doc.get("language_locked"))
    for ch in doc["chunks"]:
        if ch["status"] == "done" or (ch["status"] == "failed" and ch["attempts"] >= MAX_CHUNK_ATTEMPTS):
            continue
        ok, wait = await reserve_seconds(ch["length_s"])
        if not ok:
            until = _now() + timedelta(seconds=wait)
            await _transcripts().update_one({"id": transcript_id}, {"$set": {"status": "waiting", "waiting_until": until, "lease_until": None, "updated_at": _now()}})
            return _public(await _transcripts().find_one({"id": transcript_id}))
        if reporter is not None:
            await reporter.step(f"Writing out what was said ({ch['index'] + 1} of {len(doc['chunks'])})")
        error: Optional[str] = None
        words: list[dict] = []
        try:
            audio = await cut_audio(src, ch["start_s"], ch["length_s"])
            words, detected, ok_piece = await _transcribe_piece(audio, ch["index"], language, bool(doc.get("speakers")))
            if not ok_piece:
                error = "The speech service did not answer. This piece will be tried again."
            elif detected and not language:
                language = str(detected).lower()
                # the first piece decides the language, so every later piece is read the same way
                await _transcripts().update_one({"id": transcript_id}, {"$set": {"language": language}})
        except ChunkError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Transcription piece %s of %s failed: %s", ch["index"], transcript_id, exc)
            error = "This piece could not be transcribed. It will be tried again."
        if error:
            # the seconds were counted but nothing was transcribed: give them back so a retry is not charged twice
            await get_db()["video_transcribe_budget"].update_many(
                {"_id": {"$in": [f"hour:{_now().strftime('%Y-%m-%dT%H')}", f"day:{_now().strftime('%Y-%m-%d')}"]}}, {"$inc": {"used": -ch["length_s"]}},
            )
            await _transcripts().update_one(
                {"id": transcript_id, "chunks.index": ch["index"]},
                {"$set": {"chunks.$.status": "failed", "chunks.$.error": error}, "$inc": {"chunks.$.attempts": 1}},
            )
            ch["attempts"] += 1
            continue
        await _chunks().update_one({"transcript_id": transcript_id, "index": ch["index"]}, {"$set": {"words": words, "at": _now()}}, upsert=True)
        await _transcripts().update_one(
            {"id": transcript_id, "chunks.index": ch["index"]},
            {"$set": {"chunks.$.status": "done", "chunks.$.words": len(words), "chunks.$.error": None, "waiting_until": None}},
        )
        await _renew(transcript_id)
    return await _finish(transcript_id)


async def _finish(transcript_id: str) -> dict:
    doc = await _transcripts().find_one({"id": transcript_id})
    chunks = doc["chunks"]
    pending = [c for c in chunks if c["status"] != "done" and not (c["status"] == "failed" and c["attempts"] >= MAX_CHUNK_ATTEMPTS)]
    exhausted = [c for c in chunks if c["status"] == "failed" and c["attempts"] >= MAX_CHUNK_ATTEMPTS]
    if pending:
        # a piece failed and has tries left: it is picked up again shortly
        await _transcripts().update_one(
            {"id": transcript_id}, {"$set": {"status": "waiting", "waiting_until": _now() + timedelta(minutes=2), "lease_until": None}})
        return _public(await _transcripts().find_one({"id": transcript_id}))
    pieces = {}
    async for row in _chunks().find({"transcript_id": transcript_id}):
        pieces[row["index"]] = row["words"]
    words = merge_words([c for c in chunks if c["status"] == "done"], pieces)
    await _pages().delete_many({"transcript_id": transcript_id})
    for page, start in enumerate(range(0, len(words), PAGE_WORDS)):
        await _pages().insert_one({"transcript_id": transcript_id, "page": page, "words": words[start:start + PAGE_WORDS]})
    now = _now()
    share: dict[str, float] = {}
    spoken = {}
    for w in words:
        if w.get("sp") is not None:
            spoken[w["sp"]] = spoken.get(w["sp"], 0) + max(0, w["e_ms"] - w["s_ms"])
    total = sum(spoken.values())
    if total:
        share = {f"Speaker {int(k) + 1}" if str(k).isdigit() else str(k): round(v / total, 3) for k, v in sorted(spoken.items())}
    fields: dict[str, Any] = {
        "speaker_share": share,
        "status": "done" if not exhausted else "partial", "word_count": len(words), "has_speech": len(words) >= 3,
        "finished_at": now, "lease_until": None, "waiting_until": None, "updated_at": now,
        "error": None if not exhausted else f"{len(exhausted)} part{'s' if len(exhausted) != 1 else ''} of this video could not be transcribed.",
    }
    await _transcripts().update_one({"id": transcript_id}, {"$set": fields})
    return _public(await _transcripts().find_one({"id": transcript_id}))


async def retry_failed(transcript_id: str) -> None:
    """Give the pieces that ran out of tries another go, on their own."""
    await _transcripts().update_one(
        {"id": transcript_id},
        {"$set": {"chunks.$[c].attempts": 0, "chunks.$[c].status": "pending", "status": "queued", "lease_until": None, "error": None}},
        array_filters=[{"c.status": "failed"}],
    )


# ── reading ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

async def words_page(transcript_id: str, page: int) -> list[dict]:
    row = await _pages().find_one({"transcript_id": transcript_id, "page": max(0, page)})
    return (row or {}).get("words", [])


async def speaker_starts(workspace_id: str, media_id: str, *, max_pages: int = 60) -> list[dict]:
    """Who speaks in a recording and when each person is first heard: [{"label": "Speaker 1", "first_ms": 1200}], in the order they
    first speak. Empty when the recording has no finished transcript or the speakers were not told apart."""
    doc = await _transcripts().find_one({"workspace_id": workspace_id, "media_id": media_id, "status": {"$in": ["done", "partial"]}})
    if not doc:
        return []
    first: dict[str, int] = {}
    async for row in _pages().find({"transcript_id": doc["id"]}).sort("page", 1).limit(max_pages):
        for w in row.get("words", []):
            sp = w.get("sp")
            if sp is not None and str(sp) not in first:
                first[str(sp)] = int(w["s_ms"])
        if len(first) >= 12:
            break
    ordered = sorted(first.items(), key=lambda kv: kv[1])
    return [{"label": f"Speaker {int(k) + 1}" if k.isdigit() else k, "first_ms": ms} for k, ms in ordered]


async def all_words(transcript_id: str, limit: int = 400_000) -> list[dict]:
    out: list[dict] = []
    async for row in _pages().find({"transcript_id": transcript_id}).sort("page", 1):
        out.extend(row["words"])
        if len(out) >= limit:
            break
    return out[:limit]


_TOKEN = re.compile(r"[^\w']+", re.UNICODE)


def search(words: list[dict], query: str, *, context: int = 6, limit: int = 50) -> list[dict]:
    """Where a word or phrase is said: the time and the words around it. Matches whole words, ignoring case and marks."""
    needle = [t for t in (_TOKEN.sub("", p).casefold() for p in query.split()) if t]
    if not needle:
        return []
    keys = [_TOKEN.sub("", w["w"]).casefold() for w in words]
    hits: list[dict] = []
    n = len(needle)
    for i in range(len(keys) - n + 1):
        if keys[i:i + n] == needle:
            lo, hi = max(0, i - context), min(len(words), i + n + context)
            hits.append({"at_ms": words[i]["s_ms"], "text": " ".join(w["w"] for w in words[lo:hi]), "index": i})
            if len(hits) >= limit:
                break
    return hits


# ── carrying on without anyone watching ──────────────────────────────────────────────────────────────────────────────────

MAX_PER_TICK = 2


async def continue_waiting() -> int:
    """Pick up transcripts that are waiting for the speech allowance (or whose server went away) and carry on from the next piece.
    Called every few minutes. A finished one leaves a note in the Activity Log."""
    from app.core.scheduler_lock import distributed_job_lock  # noqa: F401 - the lock is applied by the job wrapper

    now = _now()
    due = await _transcripts().find(
        {"$or": [
            {"status": "waiting", "waiting_until": {"$lte": now}},
            {"status": {"$in": ["queued", "running"]}, "$or": [{"lease_until": None}, {"lease_until": {"$lt": now}}]},
        ]},
        {"id": 1, "workspace_id": 1, "media_id": 1, "user_id": 1},
    ).limit(MAX_PER_TICK).to_list(MAX_PER_TICK)
    resumed = 0
    from app.video import governance

    for row in due:
        if not await governance.transcription_allowed(row["workspace_id"]):
            continue                  # the workspace keeps its audio away from outside services: nothing is sent
        try:
            state = await run(row["id"])
        except Exception as exc:  # noqa: BLE001 - one transcript failing must not stop the others
            logger.error("Could not carry on transcript %s: %s", row["id"], exc)
            continue
        resumed += 1
        if state.get("status") in ("done", "partial"):
            try:
                from app.shared.activity import record_system

                await record_system(
                    workspace_id=row["workspace_id"], key=f"transcript:{row['id']}", actor_name="Ekavo", actor_user_id=row.get("user_id"),
                    category="content_generated", title="A video's transcript is ready",
                    description="Everything said in the video is written out, with timing.", status="success",
                    target_id=row["id"], target_type="Transcript", href=f"/dashboard/pipelines/video?media={row['media_id']}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not write the Activity Log entry for transcript %s: %s", row["id"], exc)
    return resumed
