"""Shared job queue helpers."""

import uuid
from typing import Any


async def enqueue_job(pipeline: str, payload: dict[str, Any]) -> str:
    """Push a new job onto the queue for the named *pipeline*.

    Args:
        pipeline: Identifier of the pipeline to execute (e.g. "text", "audio").
        payload: Serialisable dict passed to the pipeline worker.

    Returns:
        Unique job ID that can be used to poll for status.
    """
    # Placeholder: publish to Redis Stream / BullMQ / Celery and return job ID
    return str(uuid.uuid4())


async def get_job_status(job_id: str) -> dict[str, Any]:
    """Retrieve the current status and result of a queued job.

    Args:
        job_id: The job ID returned by :func:`enqueue_job`.

    Returns:
        Dict with at least ``status`` (pending | running | done | failed)
        and an optional ``result`` key.
    """
    # Placeholder: look up job_id in the queue backend and return its state
    return {"job_id": job_id, "status": "pending", "result": None}
