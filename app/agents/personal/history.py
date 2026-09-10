"""The pipeline-agnostic content-history adapter.

This is the ONLY module in the personal-assistant package that knows how any
pipeline stores its content. The persona agent asks for "this member's recent
pieces" through :func:`iter_member_content` and never touches a pipeline
collection directly.

Adding Audio/Image/Video later = append one :class:`ContentSource` to
``PIPELINE_SOURCES``. No other file in this package changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.db.mongo import content_pieces
from app.shared.pipeline_types import PipelineType


@dataclass(frozen=True)
class ContentSource:
    """How to read one pipeline's content history in a member-scoped way.

    ``collection``   — the Motor collection.
    ``text_field``   — document field holding the medium-neutral text
                        (body / transcript / caption). Used when the event
                        payload doesn't already carry ``content_text``.
    ``id_field``     — the document's stable id field.
    ``project``      — extra fields to pull for the style/quality fingerprint.
    """

    collection: Any
    text_field: str
    id_field: str
    created_field: str = "created_at"
    quality_field: str = "quality_passed"
    flagged_field: str = "flagged_for_review"


# pipeline_type value  ->  where/how its content lives.
# TEXT is the only live entry. The registry — not any agent branch — is the
# single pipeline-aware seam in Layer 1.
PIPELINE_SOURCES: dict[str, ContentSource] = {
    PipelineType.TEXT.value: ContentSource(
        collection=content_pieces,
        text_field="content",
        id_field="piece_id",
    ),
    # PipelineType.AUDIO.value: ContentSource(collection=audio_pieces, text_field="transcript", id_field="piece_id"),
    # PipelineType.IMAGE.value: ContentSource(collection=image_pieces, text_field="caption",    id_field="piece_id"),
    # PipelineType.VIDEO.value: ContentSource(collection=video_pieces, text_field="transcript", id_field="piece_id"),
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
                {"workspace_id": workspace_id, "user_id": user_id, "deleted": {"$ne": True}},
                {
                    src.id_field: 1,
                    src.text_field: 1,
                    src.created_field: 1,
                    src.quality_field: 1,
                    src.flagged_field: 1,
                },
            )
            .sort(src.created_field, -1 if newest_first else 1)
            .limit(limit)
        )
        async for doc in cursor:
            rows.append({
                "id": doc.get(src.id_field, ""),
                "pipeline_type": pt,
                "text": doc.get(src.text_field, "") or "",
                "created_at": doc.get(src.created_field),
                "quality_passed": doc.get(src.quality_field, True),
                "flagged_for_review": doc.get(src.flagged_field, False),
            })

    rows.sort(key=lambda r: (r["created_at"] is not None, r["created_at"]), reverse=newest_first)
    return rows[:limit]
