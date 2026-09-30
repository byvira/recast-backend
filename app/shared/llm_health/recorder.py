"""Keeps a durable record of every model call without ever slowing or failing one.

Every attempt is folded into an in-memory batch: an hourly rollup (calls, successes, failures, tokens,
latency, errors by kind) and, for failures, an event with the details. A background task writes the batch
to MongoDB every few seconds with atomic increments, so restarts, sleeps and several workers do not lose
or double count. Successes are stored only as counts. Failures are stored as events, capped per hour per
fingerprint so a storm cannot fill the database (the rest are still counted).

Nothing here stores prompts, answers or user text: only the provider's scrubbed error message."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.shared.llm_health import classifier
from app.shared.llm_health.context import current_feature, current_prompt_path, current_request_id
from app.shared.llm_health.scrub import scrub_message

logger = logging.getLogger(__name__)

FLUSH_EVERY_S = 3.0
SWEEP_EVERY_S = 300.0
EVENT_CAP_PER_FINGERPRINT_PER_HOUR = 20
MAX_PENDING_EVENTS = 2000
MAX_PENDING_ROLLUP_KEYS = 5000
SLOW_CALL_MS = 20_000
EVENT_TTL_DAYS = 30

LATENCY_BUCKETS = (("lt500", 500), ("lt1000", 1000), ("lt2000", 2000), ("lt5000", 5000), ("lt10000", 10000))


def app_version() -> str | None:
    """The deploy this ran on: an explicit setting, else the commit Render reports."""
    import os

    from app.core.config import settings

    return getattr(settings, "APP_VERSION", None) or (os.environ.get("RENDER_GIT_COMMIT") or "")[:7] or None


def latency_bucket(ms: float) -> str:
    for name, limit in LATENCY_BUCKETS:
        if ms < limit:
            return name
    return "gte10000"


def hour_start(at: datetime) -> datetime:
    return at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


@dataclass
class Attempt:
    provider: str
    model: str
    ok: bool
    latency_ms: float
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    http_status: int | None = None
    error_class: str | None = None
    error_message: str | None = None
    retry_after_s: float | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    workspace_id: str | None = None
    feature: str | None = None
    prompt_path: str | None = None
    request_id: str | None = None
    rate_headers: dict[str, str] | None = None
    #: what happened to the call as a whole, when known: "fallback_success", "failed", "success_after_retry"
    outcome: str | None = None
    fallback_to: str | None = None
    #: False for a note about a call that was already counted (a retry that worked): the event is kept, the call is not counted twice
    count_in_rollup: bool = True


class Batch:
    """The pending, unwritten part of the record. Pure: no database, so it can be tested alone."""

    def __init__(self) -> None:
        self.rollups: dict[tuple[datetime, str, str, str], dict[str, int]] = {}
        self.events: list[dict[str, Any]] = []
        #: per day and prompt path: calls and failures, so a prompt's failure count stays exact when events are capped
        self.prompt_days: dict[tuple[datetime, str], dict[str, int]] = {}
        self._event_counts: dict[tuple[datetime, str, str, str], int] = {}
        self.dropped_events = 0

    def add(self, a: Attempt) -> str | None:
        """Folds one attempt in. Returns the failure kind, or None for a success."""
        hour = hour_start(a.at)
        feature = a.feature or current_feature()
        kind = None
        if not a.ok:
            retry = a.retry_after_s if a.retry_after_s is not None else classifier.retry_after_from_text(a.error_message)
            kind = classifier.classify(http_status=a.http_status, error_class=a.error_class, message=a.error_message, retry_after_s=retry)
            a.retry_after_s = retry

        key = (hour, a.provider, a.model, feature)
        if key not in self.rollups and len(self.rollups) >= MAX_PENDING_ROLLUP_KEYS:
            return kind  # bounded memory: a runaway number of distinct keys is dropped, not stored
        inc = self.rollups.setdefault(key, {})

        def bump(field_name: str, by: int = 1) -> None:
            inc[field_name] = inc.get(field_name, 0) + by

        if not a.count_in_rollup:
            # a note about a call that is already counted: keep the event (and the error kind), count no second call
            bump("retries" if a.ok else f"errors.{kind}")
            self._note_only(a, hour, feature, kind)
            return kind
        if a.prompt_path or current_prompt_path():
            path = a.prompt_path or current_prompt_path() or ""
            day = a.at.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            row = self.prompt_days.setdefault((day, path), {})
            row["calls"] = row.get("calls", 0) + 1
            if not a.ok:
                row["failures"] = row.get("failures", 0) + 1
                if kind:
                    row[f"errors.{kind}"] = row.get(f"errors.{kind}", 0) + 1
        bump("calls")
        bump("successes" if a.ok else "failures")
        bump("tokens_in", int(a.tokens_in or 0))
        bump("tokens_out", int(a.tokens_out or 0))
        bump("cached_tokens", int(a.cached_tokens or 0))
        bump("latency_sum_ms", int(a.latency_ms))
        bump("latency_count")
        bump(f"latency_buckets.{latency_bucket(a.latency_ms)}")
        if kind:
            bump(f"errors.{kind}")
        if a.outcome == "fallback_success":
            bump("fallbacks")

        notable = (not a.ok) or a.latency_ms >= SLOW_CALL_MS or a.outcome in {"fallback_success", "success_after_retry"}
        if notable:
            fingerprint = (hour, a.provider, a.model, kind or a.outcome or "slow")
            seen = self._event_counts.get(fingerprint, 0)
            self._event_counts[fingerprint] = seen + 1
            if seen >= EVENT_CAP_PER_FINGERPRINT_PER_HOUR or len(self.events) >= MAX_PENDING_EVENTS:
                self.dropped_events += 1
            else:
                self.events.append(self._event(a, feature, kind))
        return kind

    def _note_only(self, a: Attempt, hour: datetime, feature: str, kind: str | None) -> None:
        fingerprint = (hour, a.provider, a.model, a.outcome or "note")
        seen = self._event_counts.get(fingerprint, 0)
        self._event_counts[fingerprint] = seen + 1
        if seen >= EVENT_CAP_PER_FINGERPRINT_PER_HOUR or len(self.events) >= MAX_PENDING_EVENTS:
            self.dropped_events += 1
        else:
            self.events.append(self._event(a, feature, kind))

    @staticmethod
    def _event(a: Attempt, feature: str, kind: str | None) -> dict[str, Any]:
        from app.core.config import settings

        return {
            "at": a.at,
            "request_id": a.request_id,
            "provider": a.provider,
            "model": a.model,
            "feature": feature,
            "prompt_path": a.prompt_path,
            "workspace_id": a.workspace_id,
            "outcome": a.outcome or ("failed" if not a.ok else "slow"),
            "error_type": kind,
            "http_status": a.http_status,
            "error_class": a.error_class,
            "provider_message": scrub_message(a.error_message),
            "retry_after_s": a.retry_after_s,
            "tokens_in": a.tokens_in or None,
            "tokens_out": a.tokens_out or None,
            "latency_ms": int(a.latency_ms),
            "fallback_to": a.fallback_to,
            "rate_headers": a.rate_headers or None,
            "env": getattr(settings, "ENVIRONMENT", None),
            "app_version": app_version(),
            "issue_id": None,
        }

    def is_empty(self) -> bool:
        return not self.rollups and not self.events and not self.prompt_days


class Recorder:
    def __init__(self) -> None:
        self._batch = Batch()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.recorder_errors = 0
        self.last_flush_at: float | None = None
        self.last_error: str | None = None
        self._last_sweep = 0.0
        #: per provider, the last few minutes of (time, ok, fallback) for the status chip; local to this process
        self.recent: dict[str, Any] = {}

    # -- the request path: cheap, synchronous, never raises ---------------------------------
    def record(self, attempt: Attempt) -> None:
        try:
            attempt.feature = attempt.feature or current_feature()
            attempt.prompt_path = attempt.prompt_path or current_prompt_path()
            attempt.request_id = attempt.request_id or current_request_id()
            if attempt.workspace_id is None:
                from app.shared import llm

                attempt.workspace_id = llm._current_workspace_id.get()
            self._batch.add(attempt)
            from collections import deque

            self.recent.setdefault(attempt.provider, deque(maxlen=2000)).append((time.time(), attempt.ok, attempt.outcome == "fallback_success", attempt.http_status == 401, attempt.model, int(attempt.tokens_in or 0) + int(attempt.tokens_out or 0)))
        except Exception:  # noqa: BLE001 - recording must never affect a model call
            self.recorder_errors += 1

    # -- the background writer ---------------------------------------------------------------
    async def flush(self) -> None:
        batch, self._batch = self._batch, Batch()
        if batch.is_empty():
            return
        try:
            from app.db.mongo import llm_events, llm_rollups
            from bson import ObjectId
            from pymongo import UpdateOne

            ops = []
            for (hour, provider, model, feature), inc in batch.rollups.items():
                ops.append(UpdateOne(
                    {"_id": f"{hour:%Y%m%d%H}|{provider}|{model}|{feature}"},
                    {"$inc": inc, "$setOnInsert": {"hour": hour, "provider": provider, "model": model, "feature": feature}},
                    upsert=True,
                ))
            if ops:
                await llm_rollups.bulk_write(ops, ordered=False)
            if batch.events:
                for event in batch.events:
                    event.setdefault("_id", ObjectId())
                await llm_events.insert_many(batch.events, ordered=False)
            if batch.prompt_days:
                from app.db.mongo import llm_prompt_daily

                await llm_prompt_daily.bulk_write([
                    UpdateOne(
                        {"_id": f"{day:%Y%m%d}|{path}"},
                        {"$inc": inc, "$setOnInsert": {"day": day, "prompt_path": path}},
                        upsert=True,
                    )
                    for (day, path), inc in batch.prompt_days.items()
                ], ordered=False)
            if batch.events:
                try:
                    from app.shared.llm_health import issues

                    applied = await issues.apply_events(batch.events)
                    from app.shared.llm_health import service

                    await service.send_alerts(created=applied["created_docs"], reopened=applied["reopened_docs"])
                except Exception as exc:  # noqa: BLE001 - issues are derived; the events are already saved
                    self.recorder_errors += 1
                    logger.error("LLM issue update failed: %s", exc)
            self.last_flush_at = time.time()
            self.last_error = None
        except Exception as exc:  # noqa: BLE001
            self.recorder_errors += 1
            self.last_error = scrub_message(exc, 200)
            logger.error("LLM health recorder could not save a batch: %s", exc)
            self._merge_back(batch)

    def _merge_back(self, batch: Batch) -> None:
        """Keep what could not be saved for the next try, within the same bounds."""
        for key, inc in batch.prompt_days.items():
            into = self._batch.prompt_days.setdefault(key, {})
            for name, value in inc.items():
                into[name] = into.get(name, 0) + value
        for key, inc in batch.rollups.items():
            into = self._batch.rollups.setdefault(key, {})
            for name, value in inc.items():
                into[name] = into.get(name, 0) + value
        room = MAX_PENDING_EVENTS - len(self._batch.events)
        self._batch.events[:0] = batch.events[: max(room, 0)]

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=FLUSH_EVERY_S)
            except asyncio.TimeoutError:
                pass
            await self.flush()
            if time.time() - self._last_sweep > SWEEP_EVERY_S:
                self._last_sweep = time.time()
                try:
                    from app.shared.llm_health import issues

                    await issues.auto_fix_sweep()
                    from app.shared.llm_health import service

                    await service.send_alerts(created=[], reopened=[])
                except Exception as exc:  # noqa: BLE001
                    logger.error("LLM issue sweep failed: %s", exc)
        await self.flush()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._run(), name="llm-health-recorder")

    async def stop(self) -> None:
        if self._task:
            self._stop.set()
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except Exception:  # noqa: BLE001
                pass
            self._task = None

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._task and not self._task.done()),
            "recorder_errors": self.recorder_errors,
            "last_flush_at": self.last_flush_at,
            "last_error": self.last_error,
            "pending_events": len(self._batch.events),
        }


recorder = Recorder()


def expiry_cutoff(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(days=EVENT_TTL_DAYS)
