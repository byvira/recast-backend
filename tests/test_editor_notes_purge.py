"""Notes on work that is gone are cleared, and notes on live or recently hidden work are left alone."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db.mongo import get_db
from app.workers.editor_notes_purge import removable


def test_rule_keeps_live_work_and_recently_hidden_work_and_drops_the_rest():
    by_target = {"alive": ["n1"], "gone": ["n2", "n3"], "hidden": ["n4", "n5"]}
    out = removable(by_target, alive={"alive"}, hidden={"hidden"}, old={"n5"})
    assert sorted(out) == ["n2", "n3", "n5"]


async def test_notes_on_a_removed_video_project_are_cleared_but_a_live_one_keeps_its_notes():
    from app.workers.editor_notes_purge import purge_orphan_notes

    notes = get_db()["editor_comments"]
    video_projects = get_db()["video_projects"]
    ws = uuid4().hex
    live, gone = uuid4().hex, uuid4().hex
    await video_projects.insert_one({"id": live, "workspace_id": ws, "deleted": False})
    now = datetime.now(timezone.utc)
    for target in (live, gone):
        await notes.insert_one({"id": uuid4().hex, "workspace_id": ws, "target_type": "video", "target_id": target, "text": "x", "author_id": "u",
                                "author_name": "U", "created_at": now, "replies": []})
    removed = await purge_orphan_notes()
    assert removed.get("video", 0) >= 1
    assert await notes.count_documents({"workspace_id": ws, "target_id": live}) == 1
    assert await notes.count_documents({"workspace_id": ws, "target_id": gone}) == 0

    await video_projects.update_one({"id": live}, {"$set": {"deleted": True}})
    await notes.update_many({"workspace_id": ws}, {"$set": {"created_at": now - timedelta(days=40)}})
    await purge_orphan_notes()
    assert await notes.count_documents({"workspace_id": ws}) == 0
