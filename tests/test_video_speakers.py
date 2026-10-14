"""The share of the talk each speaker has, worked out when a transcript finishes."""
from uuid import uuid4

from app.db.mongo import get_db
from app.video import transcribe


async def test_each_speakers_share_of_the_talk_is_worked_out_when_the_transcript_finishes():
    tid, ws, media = str(uuid4()), f"ws-{uuid4()}", str(uuid4())
    await get_db()["video_transcripts"].insert_one({
        "id": tid, "workspace_id": ws, "media_id": media, "status": "running", "language": "en", "duration_s": 60.0, "speakers": True,
        "chunks": [{"index": 0, "start_s": 0, "length_s": 60, "owns_from_s": 0, "owns_to_s": 60, "status": "done", "attempts": 0, "words": 4}],
    })
    await get_db()["video_transcript_chunks"].insert_one({"transcript_id": tid, "index": 0, "words": [
        {"w": "hello", "s_ms": 0, "e_ms": 3000, "sp": "0"}, {"w": "there", "s_ms": 3000, "e_ms": 6000, "sp": "0"},
        {"w": "hi", "s_ms": 6000, "e_ms": 7000, "sp": "1"}, {"w": "yes", "s_ms": 7000, "e_ms": 9000, "sp": "0"},
    ]})
    state = await transcribe._finish(tid)
    assert state["status"] == "done"
    assert state["speaker_share"] == {"Speaker 1": 0.889, "Speaker 2": 0.111}


async def test_a_transcript_without_speaker_labels_has_no_shares():
    tid, ws = str(uuid4()), f"ws-{uuid4()}"
    await get_db()["video_transcripts"].insert_one({
        "id": tid, "workspace_id": ws, "media_id": str(uuid4()), "status": "running", "language": "en", "duration_s": 10.0,
        "chunks": [{"index": 0, "start_s": 0, "length_s": 10, "owns_from_s": 0, "owns_to_s": 10, "status": "done", "attempts": 0, "words": 3}],
    })
    await get_db()["video_transcript_chunks"].insert_one({"transcript_id": tid, "index": 0, "words": [
        {"w": "one", "s_ms": 0, "e_ms": 400}, {"w": "two", "s_ms": 500, "e_ms": 900}, {"w": "three", "s_ms": 1000, "e_ms": 1400}]})
    assert (await transcribe._finish(tid))["speaker_share"] == {}
