"""The scheduler asks every workspace's posts which are due and which are stuck. Those queries must use an index, not read the whole collection."""

import json
from datetime import datetime, timezone

from app.db.mongo import content_pieces
from app.workers.scheduled_posts import _due_filter


async def _stages(query: dict) -> str:
    plan = await content_pieces.find(query).explain()
    return json.dumps(plan["queryPlanner"]["winningPlan"], default=str)


async def test_the_due_posts_query_uses_an_index():
    plan = await _stages(_due_filter(datetime.now(timezone.utc)))
    assert "IXSCAN" in plan and "COLLSCAN" not in plan


async def test_the_stuck_posts_query_uses_an_index():
    plan = await _stages({"publish_status": "publishing", "publishing_started_at": {"$lt": datetime.now(timezone.utc)}})
    assert "IXSCAN" in plan and "COLLSCAN" not in plan
