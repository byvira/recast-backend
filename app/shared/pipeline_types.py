"""Canonical pipeline-type identifier — the single seam the whole agent layer
depends on to stay pipeline-agnostic.

Every content/pipeline event, every persona history read, and every supervisor
digest keys off this enum. Adding Audio/Image/Video later is a one-line change
here (plus registering the new content source in
``app/agents/personal/history.py``) — no agent, event-schema, or DB-query code
changes anywhere.
"""

from __future__ import annotations

from enum import Enum


class PipelineType(str, Enum):
    """Which content pipeline an event originated from.

    Only ``TEXT`` is live today. The other members exist so the event schema,
    indexes, and agent logic are already generic — a future pipeline emits the
    same envelope with its own value and needs zero downstream changes.
    """

    TEXT = "text"
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"


# Pipelines that actually have a running implementation right now. Used only for
# sanity logging / smoke checks — never branch agent behaviour on this.
LIVE_PIPELINES: frozenset[PipelineType] = frozenset({PipelineType.TEXT})

ALL_PIPELINES: frozenset[PipelineType] = frozenset(PipelineType)


def coerce_pipeline_type(value: "PipelineType | str | None") -> PipelineType | None:
    """Normalise a caller-supplied pipeline type to the enum (or ``None``).

    ``None`` is a legitimate value for non-pipeline events (member/role/tier/
    brand changes) and is passed straight through. Any unknown string raises —
    a producer must name a real pipeline, it can never silently default to text.
    """
    if value is None:
        return None
    if isinstance(value, PipelineType):
        return value
    return PipelineType(value)  # raises ValueError on anything unrecognised
