"""Tests for the live (SSE) persistence helpers in app/pipelines/text/storage.py:
ensure_session_exists() and save_live_piece(). These are the fix for the
Module 2 root cause — every SSE-generated card used to carry piece_id=""
because nothing was ever saved for that path, which made approve, refine,
rescore, and version history all silently unreachable for real usage
despite being fully built. These are pure Mongo operations, no LLM
involved, so they're plain async integration tests against the isolated
test database, not mocked.
"""

from uuid import uuid4

from app.pipelines.text.storage import (
    ensure_session_exists,
    save_live_piece,
    get_session,
    get_piece,
    get_versions,
)


def _ids():
    return {
        "session_id": str(uuid4()),
        "workspace_id": str(uuid4()),
        "user_id": str(uuid4()),
        "brand_id": str(uuid4()),
    }


async def test_ensure_session_exists_creates_a_real_session():
    ids = _ids()
    await ensure_session_exists(
        session_id=ids["session_id"],
        workspace_id=ids["workspace_id"],
        user_id=ids["user_id"],
        brand_id=ids["brand_id"],
        source_type="text",
        goal="educate",
        tone="brand",
    )

    session = await get_session(ids["session_id"], ids["workspace_id"])
    assert session is not None
    assert session["workspace_id"] == ids["workspace_id"]
    assert session["source_type"] == "text"
    assert session["goal"] == "educate"
    assert session["pieces_count"] == 0
    assert session["pieces"] == []


async def test_ensure_session_exists_is_idempotent_across_concurrent_platforms():
    """Two platforms for the same generation run both call this before
    saving their own piece — the second call must not clobber the first,
    since it arrives with the same session_id but is otherwise a no-op."""
    ids = _ids()
    await ensure_session_exists(
        session_id=ids["session_id"],
        workspace_id=ids["workspace_id"],
        user_id=ids["user_id"],
        brand_id=ids["brand_id"],
        source_type="text",
        goal="educate",
        tone="brand",
    )
    # Second "platform" calls it again — different goal/tone would be a bug
    # if it ever happened, but the real invariant is just: still one session.
    await ensure_session_exists(
        session_id=ids["session_id"],
        workspace_id=ids["workspace_id"],
        user_id=ids["user_id"],
        brand_id=ids["brand_id"],
        source_type="text",
        goal="promote",
        tone="casual",
    )

    session = await get_session(ids["session_id"], ids["workspace_id"])
    assert session is not None
    assert session["goal"] == "educate"  # first call's value wins, not overwritten


async def test_save_live_piece_returns_a_real_persisted_piece_id():
    ids = _ids()
    await ensure_session_exists(
        session_id=ids["session_id"],
        workspace_id=ids["workspace_id"],
        user_id=ids["user_id"],
        brand_id=ids["brand_id"],
        source_type="text",
    )

    piece_id = await save_live_piece(
        session_id=ids["session_id"],
        workspace_id=ids["workspace_id"],
        user_id=ids["user_id"],
        brand_id=ids["brand_id"],
        platform="linkedin",
        content="Real generated content for real.",
        word_count=5,
        char_count=33,
        quality_passed=True,
    )

    assert piece_id
    assert piece_id != ""

    piece = await get_piece(piece_id, ids["workspace_id"])
    assert piece is not None
    assert piece["content"] == "Real generated content for real."
    assert piece["platform"] == "linkedin"
    assert piece["approval_status"] == "pending"
    assert piece["version_count"] == 1
    assert piece["deleted"] is False


async def test_save_live_piece_creates_a_real_version_1():
    ids = _ids()
    await ensure_session_exists(
        session_id=ids["session_id"],
        workspace_id=ids["workspace_id"],
        user_id=ids["user_id"],
        brand_id=ids["brand_id"],
        source_type="text",
    )
    piece_id = await save_live_piece(
        session_id=ids["session_id"],
        workspace_id=ids["workspace_id"],
        user_id=ids["user_id"],
        brand_id=ids["brand_id"],
        platform="twitter",
        content="First real version.",
        word_count=3,
        char_count=20,
    )

    versions = await get_versions(piece_id, ids["workspace_id"])
    assert len(versions) == 1
    assert versions[0]["version_number"] == 1
    assert versions[0]["action"] == "original"
    assert versions[0]["content"] == "First real version."


async def test_save_live_piece_updates_session_platforms_and_pieces_count():
    ids = _ids()
    await ensure_session_exists(
        session_id=ids["session_id"],
        workspace_id=ids["workspace_id"],
        user_id=ids["user_id"],
        brand_id=ids["brand_id"],
        source_type="text",
    )
    await save_live_piece(
        session_id=ids["session_id"], workspace_id=ids["workspace_id"],
        user_id=ids["user_id"], brand_id=ids["brand_id"],
        platform="linkedin", content="A", word_count=1, char_count=1,
    )
    await save_live_piece(
        session_id=ids["session_id"], workspace_id=ids["workspace_id"],
        user_id=ids["user_id"], brand_id=ids["brand_id"],
        platform="twitter", content="B", word_count=1, char_count=1,
    )

    session = await get_session(ids["session_id"], ids["workspace_id"])
    assert session["pieces_count"] == 2
    assert set(session["platforms"]) == {"linkedin", "twitter"}
    assert len(session["pieces"]) == 2


async def test_save_live_piece_is_workspace_scoped():
    ids = _ids()
    other_workspace_id = str(uuid4())
    await ensure_session_exists(
        session_id=ids["session_id"],
        workspace_id=ids["workspace_id"],
        user_id=ids["user_id"],
        brand_id=ids["brand_id"],
        source_type="text",
    )
    piece_id = await save_live_piece(
        session_id=ids["session_id"], workspace_id=ids["workspace_id"],
        user_id=ids["user_id"], brand_id=ids["brand_id"],
        platform="linkedin", content="scoped", word_count=1, char_count=6,
    )

    assert await get_piece(piece_id, other_workspace_id) is None
    assert await get_piece(piece_id, ids["workspace_id"]) is not None


async def test_save_live_piece_defaults_match_save_pipeline_result_shape():
    """A live-saved piece must be indistinguishable from one saved by the
    blocking /generate route — same fields, same defaults — since every
    downstream reader (approve, refine, Drafts/Library) treats both
    identically regardless of which path created them."""
    ids = _ids()
    await ensure_session_exists(
        session_id=ids["session_id"], workspace_id=ids["workspace_id"],
        user_id=ids["user_id"], brand_id=ids["brand_id"], source_type="topic",
    )
    piece_id = await save_live_piece(
        session_id=ids["session_id"], workspace_id=ids["workspace_id"],
        user_id=ids["user_id"], brand_id=ids["brand_id"],
        platform="instagram", content="x", word_count=1, char_count=1,
    )
    piece = await get_piece(piece_id, ids["workspace_id"])

    assert piece["publish_status"] == "pending"
    assert piece["publish_target"] is None
    assert piece["publish_job_id"] is None
    assert piece["hooks"] == []
    assert piece["seo"] == {}
    assert piece["quality_issues"] == []
    assert piece["flagged_for_review"] is False
    assert piece["repurposed"] is False
