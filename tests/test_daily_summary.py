"""The daily note in each workspace's Activity Log: plain counts of the day before, nothing on a quiet day, one note a day however often
the job runs. It reads only work already recorded, so no model is called."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db.mongo import get_db
from app.workers import daily_summary as D
from tests.conftest import create_workspace


def _counts(**kw):
    return {"entries": 5, "published": 0, "failed": 0, "remy": 0, "odette": 0, "waiting": 0, **kw}


def test_the_note_says_only_what_happened_in_plain_words():
    assert D.summary_text(_counts(published=3, failed=1, remy=2, odette=1, waiting=4)) == (
        "3 posts went out. 1 thing did not work and may need a look. Remy had 2 suggestions about your voice and writing. "
        "Odette raised 1 note about your publishing. 4 items are waiting for you to decide."
    )
    assert D.summary_text(_counts(published=1)) == "1 post went out."
    assert D.summary_text(_counts(entries=7)) == "7 things happened, all of them fine."
    assert D.summary_text(_counts(waiting=1)) == "1 item is waiting for you to decide."


async def _seed(ws: str, day: datetime, n: int, **extra) -> None:
    rows = [
        {"_id": uuid4().hex, "workspace_id": ws, "occurred_at": day + timedelta(hours=1 + i), "lane": "passive", "status": "success",
         "source": {"kind": "event", "id": str(i), "type": "x"}, "category": "content_generated", **extra}
        for i in range(n)
    ]
    if rows:
        await get_db()["activity_entries"].insert_many(rows)


async def test_a_busy_day_gets_one_note_and_a_quiet_day_gets_none(signup_user):
    client, _ = await signup_user("Maker")
    busy = await create_workspace(client, f"Busy {uuid4().hex[:6]}")
    quiet = await create_workspace(client, f"Quiet {uuid4().hex[:6]}")
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    await _seed(busy, yesterday, 4)
    await _seed(busy, yesterday, 2, status="failed")
    await _seed(busy, yesterday, 1, source={"kind": "remy_signal", "id": "s1", "type": "voice"})
    await _seed(quiet, yesterday, 1)

    assert await D.write_summary(busy, yesterday) is True
    assert await D.write_summary(quiet, yesterday) is False, "one thing is not worth a note"
    key = f"system:daily-summary:{busy}:{yesterday.date().isoformat()}"
    row = await get_db()["activity_entries"].find_one({"_id": key})
    assert row and row["category"] == "daily_summary" and row["status"] == "warning", "something failed, so the note says so"
    assert "did not work" in row["description"] and "Remy had 1 suggestion" in row["description"]
    assert row["metadata"]["counts"]["failed"] == 2
    # running it again the same day updates the same note, it never adds a second
    assert await D.write_summary(busy, yesterday) is True
    assert await get_db()["activity_entries"].count_documents({"_id": key}) == 1
    assert await get_db()["activity_entries"].count_documents({"workspace_id": quiet, "category": "daily_summary"}) == 0


async def test_the_job_writes_for_every_workspace_that_had_activity(signup_user):
    client, _ = await signup_user("Maker")
    ws = await create_workspace(client, f"Job {uuid4().hex[:6]}")
    yesterday = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    await _seed(ws, yesterday, 5)
    written = await D.write_daily_summaries()
    assert written >= 1
    assert await get_db()["activity_entries"].find_one({"_id": f"system:daily-summary:{ws}:{yesterday.date().isoformat()}"})
