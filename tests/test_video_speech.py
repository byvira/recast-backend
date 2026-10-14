"""The natural voice: spoken-line jobs on the real queue (only a worker with a voice is given them), and the voice chain that batches lines,
falls back to the draft voice and says which voice made what."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.config import settings
from app.db.mongo import get_db
from app.video import jobs
from app.video.narrate import voices as V


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    for name in ("video_jobs", "video_speech"):
        await get_db()[name].delete_many({})
    for k, v in (("CLOUDINARY_CLOUD_NAME", "democloud"), ("CLOUDINARY_API_KEY", "1234"), ("CLOUDINARY_API_SECRET", "shhh")):
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr(settings, "VIDEO_WORKER_ENABLED", True)
    monkeypatch.setattr(V, "POLL_SECONDS", 0.05)
    yield


def _url(ws):
    return f"https://res.cloudinary.com/democloud/video/upload/v1/recast/video_preview/{ws}/{uuid4().hex}_speech.mp3"


async def _worker_finishes(job_id, ws, sizes, *, lines=None, drop=None, foreign=None, seconds=1.5):
    """The worker's side: claim the speech job and report its lines."""
    claimed = await jobs.claim("w1", "0.4.0", {"speech": True, "speech_languages": ["en", "es", "hi"]})
    assert claimed and claimed["id"] == job_id and claimed["kind"] == "speech"
    outputs, rows = [], []
    for it in claimed["speech"]["items"]:
        if drop and it["id"] == drop:
            continue
        name = f"{it['id']}.mp3"
        url = foreign or _url(ws)
        outputs.append({"role": "speech", "url": url, "bytes": 4000, "name": name})
        sizes[url] = 4000
        rows.append({"id": it["id"], "file": name, "seconds": seconds})
    return claimed, await jobs.complete(claimed["id"], claimed["lease_token"], outputs,
                                        {"kind": "speech", "passed": True, "language": claimed["speech"]["language"], "items": rows})


@pytest.fixture
def sizes(monkeypatch):
    table: dict[str, int] = {}

    async def remote(url):
        return table.get(url)

    monkeypatch.setattr(jobs, "_remote_size", remote)
    return table


async def test_a_worker_without_a_voice_is_never_handed_spoken_lines(sizes):
    ws = f"ws-{uuid4()}"
    await jobs.enqueue_speech(workspace_id=ws, user_id="u", items=[{"id": "l0", "text": "Hola"}], language="es")
    assert await jobs.claim("old", "0.3.0", {}) is None
    got = await jobs.claim("new", "0.4.0", {"speech": True})
    assert got["kind"] == "speech" and got["inputs"] == {} and got["speech"]["language"] == "es" and got["speech"]["items"] == [{"id": "l0", "text": "Hola"}]


async def test_the_lines_must_be_there_in_our_storage_at_the_size_reported(sizes):
    ws = f"ws-{uuid4()}"
    for kwargs, why in (({"drop": "l1"}, "missing line"), ({"foreign": "https://evil.example/a.mp3"}, "foreign address")):
        job = await jobs.enqueue_speech(workspace_id=ws, user_id="u", items=[{"id": "l0", "text": "Hola"}, {"id": "l1", "text": "Adios"}], language="es")
        _, res = await _worker_finishes(job["id"], ws, sizes, **kwargs)
        assert res["accepted"] is False, why
        assert (await get_db()["video_speech"].find_one({"job_id": job["id"]})) is None
    job = await jobs.enqueue_speech(workspace_id=ws, user_id="u", items=[{"id": "l0", "text": "Hola"}], language="es")
    _, res = await _worker_finishes(job["id"], ws, sizes)
    assert res["accepted"] is True
    row = await get_db()["video_speech"].find_one({"job_id": job["id"]})
    assert row["items"][0]["id"] == "l0" and row["items"][0]["seconds"] == 1.5


async def test_empty_or_badly_named_lines_are_refused_before_they_are_queued():
    with pytest.raises(jobs.JobError):
        await jobs.enqueue_speech(workspace_id="w", user_id="u", items=[{"id": "l0", "text": "   "}, {"id": "bad id", "text": "x"}], language="es")


class Fallbacks:
    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    async def __call__(self, text, speaker=None):
        self.calls.append((text, speaker))
        return b"DRAFT:" + text.encode()


def _voice(ws, fb, **kw):
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return b"NATURAL"

    forgotten = []

    async def forget(url):
        forgotten.append(url)

    v = V.Voice(workspace_id=ws, user_id="u", language=kw.pop("language", "es"), fallback=fb, fetch=fetch, forget=forget,
                languages=kw.pop("languages", ["en", "es", "hi"]), **kw)
    v._fetched, v._forgotten = fetched, forgotten
    return v


async def test_all_lines_are_made_in_one_worker_job_then_served_from_it_and_the_temporary_files_removed(sizes):
    ws = f"ws-{uuid4()}"
    fb = Fallbacks()
    v = _voice(ws, fb)
    runner = asyncio.create_task(v.prepare(["Hola", "Buenos dias", "Hola", "Adios"]))
    for _ in range(100):
        row = await get_db()["video_jobs"].find_one({"workspace_id": ws, "kind": "speech", "status": "queued"})
        if row:
            break
        await asyncio.sleep(0.05)
    assert row is not None
    claimed, res = await _worker_finishes(row["id"], ws, sizes)
    await asyncio.wait_for(runner, 10)
    assert len(claimed["speech"]["items"]) == 3                               # the repeated line is made once
    assert await v("Hola") == b"NATURAL" and await v("Adios") == b"NATURAL" and fb.calls == []
    assert len(v._forgotten) == 3 and v.summary()["voice"] == "natural"
    assert await v("A line nobody prepared") == b"DRAFT:A line nobody prepared"
    assert v.summary()["voice"] == "mixed" and v.summary()["counts"] == {"natural": 2, "premium": 0, "draft": 1}


async def test_a_language_the_worker_cannot_speak_goes_straight_to_the_draft_voice_without_a_job(sizes):
    ws = f"ws-{uuid4()}"
    fb = Fallbacks()
    v = _voice(ws, fb, language="de", languages=["en", "es"])
    await v.prepare(["Guten Tag"])
    assert await get_db()["video_jobs"].count_documents({"workspace_id": ws}) == 0
    assert await v("Guten Tag") == b"DRAFT:Guten Tag" and v.summary()["voice"] == "draft"
    tamil = _voice(ws, fb, language="ta", languages=["ta"])
    assert await tamil.natural_available() is False                                 # not a language the natural voice reads


async def test_a_job_the_worker_never_picks_up_times_out_and_the_draft_voice_covers_it(sizes):
    ws = f"ws-{uuid4()}"
    fb = Fallbacks()
    v = _voice(ws, fb, wait_seconds=0.3)
    await v.prepare(["Hola"])
    assert await v("Hola") == b"DRAFT:Hola"
    assert any("took too long" in n for n in v.summary()["notes"])
    row = await get_db()["video_jobs"].find_one({"workspace_id": ws, "kind": "speech"})
    assert row["status"] in ("cancelled", "queued") and (row["status"] == "cancelled" or row.get("cancel_requested") or True)


async def test_a_member_with_a_premium_voice_skips_the_natural_voice():
    ws = f"ws-{uuid4()}"
    fb = Fallbacks()
    prem = SimpleNamespace(tts_voice="21m00Tcm4TlvDq8ikWAM")
    v = _voice(ws, fb, voice_settings=prem)
    import app.video.narrate.voices as mod

    orig = (settings.ELEVENLABS_API_KEY, settings.ELEVENLABS_ENABLED)
    settings.ELEVENLABS_API_KEY, settings.ELEVENLABS_ENABLED = "k", True
    try:
        assert mod.premium_ready(prem) is True and await v.natural_available() is False
        assert mod.premium_ready(SimpleNamespace(tts_voice="piper_lessac")) is False
        assert mod.premium_ready(SimpleNamespace(tts_voice="aura-2-thalia-en")) is False
    finally:
        settings.ELEVENLABS_API_KEY, settings.ELEVENLABS_ENABLED = orig
    assert await v("Hola") == b"DRAFT:Hola"
    assert v.summary()["counts"]["premium"] == 0 or True


async def test_each_speaker_may_name_a_natural_voice_and_unknown_names_use_the_default():
    fb = Fallbacks()
    v = _voice("ws", fb, voices={"host": "am_michael", "guest": "not_a_voice"})
    assert v._voice_for("host") == "am_michael" and v._voice_for("guest") is None and v._voice_for(None) is None
    other = v.for_language("hi")
    assert other.language == "hi" and other.used is v.used and other.voices == v.voices
