"""The workspace event bus.

``emit_event`` is the single seam every producer uses — API routes, the storage
layer, and future pipelines all call this one function. It:

1. Validates the envelope + payload (fails closed: a bad event is written
   *nowhere*).
2. Inserts the envelope into ``workspace_events`` — the durable source of truth
   and replay store (90-day TTL).
3. Mirrors it onto the ``recast:events`` Redis Stream, which the two agent
   workers consume via independent consumer groups.

**It never raises into the caller.** Everything after validation is wrapped so a
Mongo/Redis hiccup can't break a request or a pipeline run. Call it off the
await path with :func:`emit_event_background` when latency matters.

Redis Streams (not pub/sub) is deliberate: consumer groups + ``XACK``-after-
commit + ``XAUTOCLAIM`` give at-least-once delivery and crash recovery. Pub/sub
would silently drop every event emitted while a worker is redeploying.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from pymongo.errors import DuplicateKeyError

from app.db.mongo import workspace_events
from app.db.redis import get_redis
from app.models.agent_events import PAYLOAD_MODELS, EventType, WorkspaceEvent
from app.shared.pipeline_types import PipelineType, coerce_pipeline_type

logger = logging.getLogger(__name__)

#: Redis Stream key. One stream for the whole deployment; consumers filter by the
#: ``workspace_id`` field on each entry.
EVENTS_STREAM = "recast:events"

#: Approximate cap on stream length. The Mongo collection is the real history;
#: the stream only needs enough depth to cover a worker outage.
#: Provisional — tune once real event throughput is known.
STREAM_MAXLEN = 50_000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_payload(event_type: EventType, payload: dict[str, Any]) -> None:
    """Run the payload dict through its typed model. Raises ``ValidationError``
    if it doesn't fit — caught by :func:`emit_event` and turned into a
    fail-closed no-op with a loud log line."""
    model = PAYLOAD_MODELS.get(event_type)
    if model is not None:
        model.model_validate(payload)


async def emit_event(
    *,
    event_type: "EventType | str",
    pipeline_type: "PipelineType | str | None",   # required kwarg — NO default
    workspace_id: str,
    actor_user_id: str,
    actor_role: str = "",
    payload: "dict[str, Any] | BaseModel | None" = None,
    idempotency_key: str | None = None,
    occurred_at: datetime | None = None,
) -> str | None:
    """Emit one workspace event. Returns the ``event_id`` on success, ``None`` on
    a duplicate (idempotency) or any failure. Never raises.

    ``pipeline_type`` has no default on purpose: pass ``PipelineType.TEXT`` (etc.)
    for content/pipeline events, ``None`` for member/role/tier/brand events. A
    content event with ``pipeline_type=None`` is rejected by the envelope model.
    """
    try:
        et = event_type if isinstance(event_type, EventType) else EventType(event_type)
        pt = coerce_pipeline_type(pipeline_type)

        if isinstance(payload, BaseModel):
            payload_dict: dict[str, Any] = payload.model_dump(mode="json")
        else:
            payload_dict = dict(payload or {})

        # Fail closed on a malformed payload — before any I/O.
        _validate_payload(et, payload_dict)

        event = WorkspaceEvent(
            event_type=et,
            pipeline_type=pt,
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            occurred_at=occurred_at or _utcnow(),
            ingested_at=_utcnow(),
            idempotency_key=idempotency_key or str(uuid4()),
            payload=payload_dict,
        )
    except (ValidationError, ValueError) as exc:
        logger.error(
            "emit_event: rejected invalid event type=%s pipeline_type=%r ws=%s: %s",
            event_type, pipeline_type, workspace_id, exc,
        )
        return None

    doc = event.model_dump(mode="json")
    doc["_id"] = event.event_id
    # Store ingested_at as a real BSON date so the TTL index on it actually
    # expires documents (model_dump(mode="json") would make it a string).
    doc["ingested_at"] = event.ingested_at

    # ── 1. Durable write (source of truth) ───────────────────────────────
    try:
        await workspace_events.insert_one(doc)
    except DuplicateKeyError:
        logger.debug(
            "emit_event: duplicate idempotency_key=%s — skipping",
            event.idempotency_key,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — must not propagate
        logger.error("emit_event: Mongo insert failed (event dropped): %s", exc)
        return None

    # ── 2. Mirror onto the Redis Stream ─────────────────────────────────
    # A stream failure is non-fatal: the event is safely in Mongo and a worker
    # can backfill from there. We log loudly so it isn't missed.
    try:
        redis = await get_redis()
        await redis.xadd(
            EVENTS_STREAM,
            {
                "data": json.dumps(doc, default=str),
                "event_id": event.event_id,
                "event_type": event.event_type.value,
                "workspace_id": event.workspace_id,
                "pipeline_type": event.pipeline_type.value if event.pipeline_type else "",
            },
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "emit_event: Redis XADD failed for event %s (persisted, not streamed): %s",
            event.event_id, exc,
        )

    return event.event_id


def _log_task_result(task: "asyncio.Task[str | None]") -> None:
    try:
        task.result()
    except Exception as exc:  # noqa: BLE001
        logger.error("emit_event_background task crashed: %s", exc)


def emit_event_background(**kwargs: Any) -> "asyncio.Task[str | None]":
    """Fire-and-forget wrapper — schedules :func:`emit_event` on the running loop
    so it never sits on a request's await path. Exceptions are logged, not raised.
    """
    task = asyncio.create_task(emit_event(**kwargs))
    task.add_done_callback(_log_task_result)
    return task
