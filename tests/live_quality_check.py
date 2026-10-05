"""A real-model check of post quality and language. NOT part of the normal run: it makes real model calls (about 30), so it only
runs when asked for:

    RUN_LIVE_LLM=1 .venv/Scripts/python.exe -m pytest tests/live_quality_check.py -q -s -p no:logging

It makes small campaigns in the test database, runs one batch each through the real pipeline with the real models (no pictures
are made), prints every post with its score, and checks that the language that was asked for is the language that came back.
"""
import json
import os
from pathlib import Path

import pytest

from app.db.mongo import content_pieces
from app.pipelines.text.quality_set import score_post
from tests.conftest import signup_new_user
from tests.test_campaigns import _create_brand, _valid_body

pytestmark = pytest.mark.skipif(os.environ.get("RUN_LIVE_LLM") != "1", reason="makes real model calls; set RUN_LIVE_LLM=1")

CASES = (
    ("en", "Why small teams lose track of customer feedback"),
    ("ta+en", "Why small teams lose track of customer feedback"),
    ("ta+en", "The cost of replying to leads too late"),
    ("hi+en", "How to plan a content week without burning out"),
)


async def _no_picture(**_kwargs):
    return None


async def test_posts_come_back_in_the_language_asked_for(api_client, monkeypatch):
    from app.pipelines.text import orchestrator

    monkeypatch.setattr(orchestrator, "pick_default_image", _no_picture)
    await signup_new_user(api_client)
    brand_id = await _create_brand(api_client)

    report_path = Path(os.environ.get("LIVE_REPORT", "live_quality_report.json"))
    scored = []
    report = []
    for language, topic in CASES:
        created = await api_client.post(
            "/api/v1/campaigns/",
            json=_valid_body(
                brand_id, name=f"Live {language}", topic_cluster=topic, platforms=["LinkedIn"], language=language,
                cadence={"frequency": "manual", "days_per_batch": 1},
            ),
        )
        assert created.status_code == 201, created.text
        campaign_id = created.json()["id"]
        res = await api_client.post(f"/api/v1/campaigns/{campaign_id}/generate-next-batch")
        assert res.status_code == 200, res.text
        for piece in await content_pieces.find({"campaign_id": campaign_id}).to_list(length=None):
            row = score_post(piece.get("content", ""), language, topic)
            scored.append((language, topic, piece, row))
            report.append({
                "language": language, "topic": topic, "stored_language": piece.get("language"),
                "flagged": piece.get("flagged_for_review"), "quality_issues": piece.get("quality_issues"),
                "score": row, "content": piece.get("content", ""),
            })
            # Written as UTF-8 text: the Windows console cannot print every character a post may carry.
            report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    assert scored, "no posts were made"
    mixed = [row for language, _t, _p, row in scored if "+" in language]
    assert sum(1 for r in mixed if r["language_ok"]) >= max(1, len(mixed) - 1), "mixed-language posts came back in plain English"
    assert all(p.get("language") == lang for lang, _t, p, _r in scored), "a post was saved with a different language"
