"""The pipeline-agnostic content-history adapter.

This is the ONLY module in the personal-assistant package that knows how any
pipeline stores its content. The persona agent asks for "this member's recent
pieces" through :func:`iter_member_content` and never touches a pipeline
collection directly.

Adding Audio/Image/Video later = append one :class:`ContentSource` to
``PIPELINE_SOURCES``. No other file in this package changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.db.mongo import audio_assets, content_pieces, image_assets, media_assets
from app.shared.pipeline_types import PipelineType


@dataclass(frozen=True)
class ContentSource:
    """How to read one pipeline's content history in a member-scoped way.

    ``collection``   — the Motor collection.
    ``text_field``   — document field holding the medium-neutral text
                        (body / transcript / caption). Used when the event
                        payload doesn't already carry ``content_text``.
    ``id_field``     — the document's stable id field.
    ``user_field``   — the document's creator field. Real, found-not-assumed
                        gap: ``content_pieces`` names this ``user_id``, but
                        ``AudioAsset``/``ImageAsset`` name the identical
                        concept ``created_by`` — a single hardcoded field
                        name here would silently return zero real rows for
                        any pipeline that doesn't happen to use ``user_id``.
    """

    collection: Any
    text_field: str
    id_field: str
    created_field: str = "created_at"
    quality_field: str = "quality_passed"
    flagged_field: str = "flagged_for_review"
    user_field: str = "user_id"
    # Tried in order when ``text_field`` is empty. An UPLOADED recording or
    # image has no script/prompt, so without this the agents saw an empty
    # string for everything a member brought themselves. Values may be plain
    # text or a word-timed transcript (list of {"word": ...}).
    fallback_fields: tuple[str, ...] = ()
    # Extra Mongo filter for a collection that holds more than this pipeline
    # (video lives in ``media_assets`` alongside every image and audio file).
    extra_filter: dict = field(default_factory=dict)


def text_from_field(value: Any) -> str:
    """Plain text from a document field: a string as-is, or a transcript /
    list of strings joined into one."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [
            (item.get("word", "") if isinstance(item, dict) else str(item)).strip()
            for item in value
        ]
        return " ".join(p for p in parts if p)
    return ""


# pipeline_type value  ->  where/how its content lives.
# TEXT, AUDIO, and IMAGE are all live (2026-09-27 — Audio/Image both got
# real backends this build; the registry entries below are the "append one
# ContentSource" this module's own docstring always said was all a new
# pipeline needs). VIDEO stays out until it has a real backend of its own.
#
# AudioAsset/ImageAsset don't have quality_passed/flagged_for_review fields
# the way ContentPiece does — they use approval_status (pending/approved/
# rejected) and qa_flagged/qa_flag_reason instead. flagged_field is mapped
# to the real qa_flagged field (the same "should a human look at this"
# signal, just named differently); quality_field is deliberately left at
# its default "quality_passed" name, which doesn't exist on these
# documents — .get() falls through to True (see iter_member_content),
# which is honestly correct: neither pipeline has an automated quality
# gate the way Text's does, so "always passed" isn't a fabrication, it's
# an accurate "no gate exists" signal.
#
# text_field picks the single real field that best represents each
# pipeline's "written content" for voice/style comparison: Audio's real
# script (empty for an UPLOADED asset with no script — degrades to an
# empty string for that row, not an error); Image's real generation
# prompt (empty for an UPLOADED image, same graceful degradation).
PIPELINE_SOURCES: dict[str, ContentSource] = {
    PipelineType.TEXT.value: ContentSource(
        collection=content_pieces,
        text_field="content",
        id_field="piece_id",
    ),
    PipelineType.AUDIO.value: ContentSource(
        collection=audio_assets,
        text_field="script",
        id_field="id",
        flagged_field="qa_flagged",
        user_field="created_by",
        fallback_fields=("transcript",),  # an uploaded recording has words, not a script
    ),
    PipelineType.IMAGE.value: ContentSource(
        collection=image_assets,
        text_field="prompt",
        id_field="id",
        flagged_field="qa_flagged",
        user_field="created_by",
        fallback_fields=("alt_text",),  # an uploaded image has a description, not a prompt
    ),
    # Video has no pipeline collection of its own — an uploaded video is a
    # MediaAsset. It only counts as content once it has been analysed (its
    # words transcribed): before that there is nothing an agent can read.
    PipelineType.VIDEO.value: ContentSource(
        collection=media_assets,
        text_field="transcript",
        id_field="id",
        user_field="created_by",
        extra_filter={"kind": "video", "analysis_status": "done"},
    ),
}


def known_pipeline(pipeline_type: Optional[str]) -> bool:
    return pipeline_type in PIPELINE_SOURCES


async def iter_member_content(
    workspace_id: str,
    user_id: str,
    *,
    pipeline_type: Optional[str] = None,
    limit: int = 30,
    newest_first: bool = True,
) -> list[dict]:
    """Return a member's recent content pieces, newest first.

    Scoped hard by ``workspace_id`` + ``user_id`` (the creator/``created_by``
    field on every pipeline's documents). ``pipeline_type=None`` fans out across
    every registered pipeline and merges by recency — this is how the persona
    stays cross-pipeline without any pipeline literal in the agent.

    Each returned dict is normalised to:
        {id, pipeline_type, text, created_at, quality_passed, flagged_for_review}
    """
    sources = (
        [(pipeline_type, PIPELINE_SOURCES[pipeline_type])]
        if pipeline_type and pipeline_type in PIPELINE_SOURCES
        else list(PIPELINE_SOURCES.items())
    )

    rows: list[dict] = []
    for pt, src in sources:
        cursor = (
            src.collection.find(
                {
                    "workspace_id": workspace_id, src.user_field: user_id,
                    "deleted": {"$ne": True}, **src.extra_filter,
                },
                {
                    src.id_field: 1,
                    src.text_field: 1,
                    src.created_field: 1,
                    src.quality_field: 1,
                    src.flagged_field: 1,
                    **{f: 1 for f in src.fallback_fields},
                },
            )
            .sort(src.created_field, -1 if newest_first else 1)
            .limit(limit)
        )
        async for doc in cursor:
            text = text_from_field(doc.get(src.text_field))
            for fallback in src.fallback_fields:
                if text:
                    break
                text = text_from_field(doc.get(fallback))
            rows.append({
                "id": doc.get(src.id_field, ""),
                "pipeline_type": pt,
                "text": text,
                "created_at": doc.get(src.created_field),
                "quality_passed": doc.get(src.quality_field, True),
                "flagged_for_review": doc.get(src.flagged_field, False),
            })

    rows.sort(key=lambda r: (r["created_at"] is not None, r["created_at"]), reverse=newest_first)
    return rows[:limit]
