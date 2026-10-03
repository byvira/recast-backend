"""A piece the graph already saved is adopted, not saved a second time (audit T-01). Plain integration tests against the
isolated test database; no model calls."""

from datetime import datetime, timezone
from uuid import uuid4

from app.db.mongo import content_pieces, content_piece_versions, content_sessions
from app.models.text import GeneratedPiece, InputSourceType, Platform, TextPipelineResult
from app.pipelines.text.storage import (
    discard_generated_piece,
    ensure_session_exists,
    save_live_piece,
    save_pipeline_result,
)


async def _graph_saved_piece(ws: str, user: str, brand: str, graph_session: str) -> str:
    await ensure_session_exists(
        session_id=graph_session, workspace_id=ws, user_id=user, brand_id=brand, source_type="text",
    )
    return await save_live_piece(
        session_id=graph_session, workspace_id=ws, user_id=user, brand_id=brand,
        platform="LinkedIn", content="A saved post.", word_count=3, char_count=13,
    )


def _result(ws: str, user: str, brand: str, session: str, piece_id: str | None) -> TextPipelineResult:
    return TextPipelineResult(
        session_id=session, workspace_id=ws, user_id=user, brand_id=brand,
        source_type=InputSourceType.TEXT, batch_day_index=2, angle="A fresh angle",
        created_at=datetime.now(timezone.utc),
        pieces=[GeneratedPiece(platform=Platform.LINKEDIN, content="A saved post.", word_count=3, char_count=13, piece_id=piece_id)],
    )


async def test_a_saved_piece_is_adopted_with_campaign_and_batch_details_and_no_copy():
    ws, user, brand = str(uuid4()), str(uuid4()), str(uuid4())
    graph_session, outer_session, campaign = str(uuid4()), str(uuid4()), str(uuid4())
    saved = await _graph_saved_piece(ws, user, brand, graph_session)

    session_id, ids = await save_pipeline_result(_result(ws, user, brand, outer_session, saved), campaign_id=campaign)

    assert ids == [saved]
    assert await content_pieces.count_documents({"workspace_id": ws}) == 1
    doc = await content_pieces.find_one({"piece_id": saved})
    assert doc["campaign_id"] == campaign and doc["batch_day_index"] == 2 and doc["angle"] == "A fresh angle"
    assert doc["session_id"] == outer_session == session_id
    # its first version moved with it, and the graph's own session is gone
    assert (await content_piece_versions.find_one({"piece_id": saved}))["session_id"] == outer_session
    assert await content_sessions.find_one({"session_id": graph_session}) is None
    assert (await content_sessions.find_one({"session_id": outer_session}))["pieces_count"] == 1


async def test_a_piece_with_no_saved_copy_is_still_saved_as_new():
    ws, user, brand = str(uuid4()), str(uuid4()), str(uuid4())
    session = str(uuid4())
    _, ids = await save_pipeline_result(_result(ws, user, brand, session, None))
    assert len(ids) == 1
    assert await content_pieces.count_documents({"workspace_id": ws}) == 1


async def test_the_same_session_may_already_exist_without_an_error():
    ws, user, brand = str(uuid4()), str(uuid4()), str(uuid4())
    session = str(uuid4())
    saved = await _graph_saved_piece(ws, user, brand, session)
    _, ids = await save_pipeline_result(_result(ws, user, brand, session, saved))
    assert ids == [saved]
    assert await content_sessions.count_documents({"session_id": session}) == 1
    assert await content_pieces.count_documents({"workspace_id": ws}) == 1


async def test_discarding_a_generated_copy_removes_its_versions_and_empty_session():
    ws, user, brand = str(uuid4()), str(uuid4()), str(uuid4())
    session = str(uuid4())
    saved = await _graph_saved_piece(ws, user, brand, session)
    await discard_generated_piece(saved, ws)
    assert await content_pieces.find_one({"piece_id": saved}) is None
    assert await content_piece_versions.find_one({"piece_id": saved}) is None
    assert await content_sessions.find_one({"session_id": session}) is None
    # a piece that is not there is not an error
    await discard_generated_piece(saved, ws)


async def test_the_default_picture_is_kept_on_a_piece_saved_through_the_result_path():
    from app.models.media import MediaAsset, MediaKind, MediaSource

    ws, user, brand = str(uuid4()), str(uuid4()), str(uuid4())
    media = MediaAsset(
        id=uuid4().hex, workspace_id=ws, kind=MediaKind.IMAGE, url="https://cdn.example/card.png", mime_type="image/png",
        source=MediaSource.RENDERED, created_by=user, created_at=datetime.now(timezone.utc),
    )
    result = _result(ws, user, brand, str(uuid4()), None)
    result.pieces[0].media = [media]
    _, ids = await save_pipeline_result(result)
    doc = await content_pieces.find_one({"piece_id": ids[0]})
    assert doc["media"] and doc["media"][0]["url"] == "https://cdn.example/card.png"
