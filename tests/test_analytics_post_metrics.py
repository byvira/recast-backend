"""Tests for fetch_metrics_node's post-metrics query.

It used to read piece["platform_results"], an array field nothing ever
wrote to content_pieces (only synthesized on the fly for the calendar API
response — see app.api.v1.analytics.get_calendar and test_calendar.py's
identical fix for that endpoint). That meant posts_to_fetch was always
empty and post_metrics never populated, regardless of how many pieces were
actually published — real code, but a broken query, not the "thin data"
R2-4 originally diagnosed it as.

Mocks fetch_post_metrics_all/fetch_account_metrics_all themselves rather
than hitting real platform APIs, same constraint every other
analytics-adjacent test in this suite works around (see
test_analytics_snapshots.py).
"""

from uuid import uuid4

from app.agents.analytics import nodes as nodes_module
from app.agents.analytics.state import build_initial_state
from app.db.mongo import content_pieces
from app.pipelines.analytics.base import PostMetrics
from app.pipelines.text.storage import ensure_session_exists, save_live_piece
from tests.conftest import create_workspace


async def _seed_published_piece(
    workspace_id: str, user_id: str, brand_id: str, platform: str, platform_post_id: str
) -> str:
    session_id = str(uuid4())
    await ensure_session_exists(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, source_type="text",
    )
    piece_id = await save_live_piece(
        session_id=session_id, workspace_id=workspace_id, user_id=user_id,
        brand_id=brand_id, platform=platform,
        content="Published test content.", word_count=3, char_count=25,
    )
    await content_pieces.update_one(
        {"piece_id": piece_id},
        {"$set": {"publish_status": "published", "platform_post_id": platform_post_id}},
    )
    return piece_id


async def test_fetch_metrics_node_finds_published_posts_from_real_fields(signup_user, monkeypatch):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Analytics Post Metrics WS")
    await _seed_published_piece(
        ws_id, profile["id"], str(uuid4()), platform="LinkedIn", platform_post_id="urn:li:share:123",
    )

    captured_posts = []

    async def _fake_fetch_post_metrics_all(workspace_id, posts):
        captured_posts.extend(posts)
        return [
            PostMetrics(
                platform="linkedin", post_id=posts[0]["piece_id"],
                platform_post_id=posts[0]["platform_post_id"], likes=5,
            )
        ]

    async def _fake_fetch_account_metrics_all(workspace_id, platforms, since, until):
        return []

    monkeypatch.setattr(nodes_module, "fetch_post_metrics_all", _fake_fetch_post_metrics_all)
    monkeypatch.setattr(nodes_module, "fetch_account_metrics_all", _fake_fetch_account_metrics_all)

    state = build_initial_state(ws_id, user_id=profile["id"])
    state["connected_platforms"] = ["linkedin"]

    result = await nodes_module.fetch_metrics_node(state)

    # The real bug: posts_to_fetch used to always be [] here, so
    # fetch_post_metrics_all was never even called with real data.
    assert len(captured_posts) == 1
    assert captured_posts[0]["platform"] == "linkedin"
    assert captured_posts[0]["platform_post_id"] == "urn:li:share:123"
    assert len(result["post_metrics"]) == 1
    assert result["post_metrics"][0]["likes"] == 5


async def test_fetch_metrics_node_skips_published_pieces_without_a_platform_post_id(signup_user, monkeypatch):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Analytics Post Metrics WS 2")
    # Published in name only — no platform_post_id ever recorded (e.g. a
    # manually marked-published draft that was never really published).
    session_id = str(uuid4())
    await ensure_session_exists(
        session_id=session_id, workspace_id=ws_id, user_id=profile["id"],
        brand_id=str(uuid4()), source_type="text",
    )
    piece_id = await save_live_piece(
        session_id=session_id, workspace_id=ws_id, user_id=profile["id"],
        brand_id=str(uuid4()), platform="LinkedIn",
        content="No real publish.", word_count=3, char_count=16,
    )
    await content_pieces.update_one({"piece_id": piece_id}, {"$set": {"publish_status": "published"}})

    captured_posts = []

    async def _fake_fetch_post_metrics_all(workspace_id, posts):
        captured_posts.extend(posts)
        return []

    async def _fake_fetch_account_metrics_all(workspace_id, platforms, since, until):
        return []

    monkeypatch.setattr(nodes_module, "fetch_post_metrics_all", _fake_fetch_post_metrics_all)
    monkeypatch.setattr(nodes_module, "fetch_account_metrics_all", _fake_fetch_account_metrics_all)

    state = build_initial_state(ws_id, user_id=profile["id"])
    state["connected_platforms"] = ["linkedin"]

    result = await nodes_module.fetch_metrics_node(state)

    assert captured_posts == []
    assert result["post_metrics"] == []
