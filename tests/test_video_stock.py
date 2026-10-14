"""Stock footage: adapters on recorded answer shapes, caching, rate-limit handling, ranking, the keyword rail, saving a clip with its
licence, and placing it in a project. The network is replaced at the HTTP call; the database and the project code are real."""
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from app.core.config import settings
from app.db.mongo import get_db, media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.video import edl
from app.video.stock import providers as P
from app.video.stock import rank as R
from app.video.stock import service as S
from tests.conftest import create_workspace

PIXABAY_HIT = {
    "id": 125, "pageURL": "https://pixabay.com/videos/id-125/", "type": "film", "tags": "city, traffic, night", "duration": 12,
    "picture_id": "1234567", "user_id": 77, "user": "Free-Videos",
    "videos": {"large": {"url": "https://cdn.pixabay.com/video/l.mp4", "width": 1920, "height": 1080, "size": 9000000},
               "medium": {"url": "https://cdn.pixabay.com/video/m.mp4", "width": 1280, "height": 720, "size": 4000000},
               "small": {"url": "https://cdn.pixabay.com/video/s.mp4", "width": 960, "height": 540, "size": 2000000},
               "tiny": {"url": "https://cdn.pixabay.com/video/t.mp4", "width": 640, "height": 360, "size": 900000}},
}
PEXELS_VIDEO = {
    "id": 9001, "url": "https://www.pexels.com/video/9001/", "image": "https://images.pexels.com/videos/9001/pic.jpg", "duration": 18,
    "user": {"id": 5, "name": "Alex Doe", "url": "https://www.pexels.com/@alex"},
    "video_files": [
        {"id": 1, "quality": "sd", "file_type": "video/mp4", "width": 640, "height": 360, "link": "https://videos.pexels.com/sd.mp4"},
        {"id": 2, "quality": "hd", "file_type": "video/mp4", "width": 1920, "height": 1080, "link": "https://videos.pexels.com/hd.mp4"},
        {"id": 3, "quality": "uhd", "file_type": "video/mp4", "width": 3840, "height": 2160, "link": "https://videos.pexels.com/uhd.mp4"},
        {"id": 4, "quality": "hls", "file_type": "application/x-mpegURL", "width": 0, "height": 0, "link": "https://videos.pexels.com/x.m3u8"},
    ],
}


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    await get_db()["video_stock_cache"].delete_many({})
    for q in P._seen.values():
        q.clear()
    monkeypatch.setattr(settings, "PIXABAY_API_KEY", "pk")
    monkeypatch.setattr(settings, "PEXELS_API_KEY", "xk", raising=False)
    yield


# ── adapters ────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_pixabay_hit_becomes_a_record_with_its_author_licence_and_a_medium_download():
    c = P.normalise_pixabay(PIXABAY_HIT)
    assert c["provider"] == "pixabay" and c["id"] == "125" and c["download_url"].endswith("/m.mp4") and c["preview_url"].endswith("/t.mp4")
    assert c["author"] == "Free-Videos" and c["license"] == "Pixabay Content License" and c["license_url"].startswith("https://pixabay.com/")
    assert c["tags"] == ["city", "traffic", "night"] and c["duration_s"] == 12.0 and c["poster_url"].endswith("1234567_640x360.jpg")
    assert P.normalise_pixabay({"id": 1, "videos": {}}) is None


def test_a_pexels_video_picks_the_best_mp4_up_to_full_hd_and_never_a_playlist():
    c = P.normalise_pexels(PEXELS_VIDEO)
    assert c["download_url"].endswith("/hd.mp4") and c["preview_url"].endswith("/sd.mp4")
    assert c["author"] == "Alex Doe" and c["license"] == "Pexels License" and c["width"] == 1920
    assert P.normalise_pexels({"id": 2, "video_files": [{"file_type": "application/x-mpegURL", "link": "x"}]}) is None


# ── search: cache, errors, rate limits ──────────────────────────────────────────────────────────────────────────────────

async def test_a_search_asks_each_source_once_and_answers_from_the_cache_the_second_time(monkeypatch):
    calls = []

    async def fake_get(provider, url, *, params=None, headers=None, attempts=3):
        calls.append(provider)
        return {"hits": [PIXABAY_HIT]} if provider == "pixabay" else {"videos": [PEXELS_VIDEO]}

    monkeypatch.setattr(P, "_get", fake_get)
    first = await P.search("busy city street")
    assert sorted(calls) == ["pexels", "pixabay"] and len(first["clips"]) == 2 and first["cached"] is False and first["errors"] == []
    second = await P.search("  Busy   City street ")
    assert len(calls) == 2 and second["cached"] is True and len(second["clips"]) == 2


async def test_one_source_failing_does_not_stop_the_other_and_the_failure_is_reported(monkeypatch):
    async def fake_get(provider, url, *, params=None, headers=None, attempts=3):
        if provider == "pixabay":
            raise P.RateLimited("pixabay", 30.0)
        return {"videos": [PEXELS_VIDEO]}

    monkeypatch.setattr(P, "_get", fake_get)
    res = await P.search("lake")
    assert [c["provider"] for c in res["clips"]] == ["pexels"] and "busy" in res["errors"][0]
    # a failure is not cached, so asking again tries again
    calls = []

    async def fixed(provider, *a, **k):
        calls.append(provider)
        return {"hits": [PIXABAY_HIT]} if provider == "pixabay" else {"videos": [PEXELS_VIDEO]}

    monkeypatch.setattr(P, "_get", fixed)
    again = await P.search("lake")
    assert "pixabay" in calls and len(again["clips"]) == 2


async def test_nothing_connected_says_so_instead_of_returning_nothing_quietly(monkeypatch):
    monkeypatch.setattr(settings, "PIXABAY_API_KEY", "")
    monkeypatch.setattr(settings, "PEXELS_API_KEY", "", raising=False)
    res = await P.search("lake")
    assert res["clips"] == [] and res.get("unconfigured") is True and "No stock library" in res["errors"][0]


async def test_a_rate_limit_answer_is_waited_out_briefly_then_reported(monkeypatch):
    class Resp:
        def __init__(self, code, headers=None, body=None):
            self.status_code, self.headers, self._b = code, headers or {}, body or {}

        def json(self):
            return self._b

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("x", request=None, response=None)

    answers = [Resp(429, {"Retry-After": "1"}), Resp(200, body={"hits": []})]
    slept: list[float] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            return answers.pop(0)

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(P.httpx, "AsyncClient", lambda **k: Client())
    monkeypatch.setattr(P.asyncio, "sleep", fake_sleep)
    assert (await P._get("pixabay", P.PIXABAY_URL, params={"q": "x"})) == {"hits": []} and slept == [1.0]
    answers[:] = [Resp(429, {"Retry-After": "30"})]
    with pytest.raises(P.RateLimited):
        await P._get("pixabay", P.PIXABAY_URL, params={"q": "x"})
    answers[:] = [Resp(403)]
    with pytest.raises(P.StockError):
        await P._get("pexels", P.PEXELS_URL)


async def test_calls_are_paced_per_provider_by_the_window():
    P._seen["pixabay"].clear()
    limit, _ = P._LIMITS["pixabay"]
    for _ in range(limit):
        assert await P._pace("pixabay") is None
    wait = await P._pace("pixabay")
    assert wait is not None and 0 < wait <= 60.0


# ── ranking and the rail ────────────────────────────────────────────────────────────────────────────────────────────────

def _clip(i, tags, dur=10, w=1920, h=1080, prov="pixabay"):
    return {"provider": prov, "id": str(i), "tags": tags, "duration_s": dur, "width": w, "height": h}


def test_clips_are_ranked_by_shared_words_length_shape_and_quality_with_reasons():
    line = "Traffic builds up in the city every evening"
    ranked = R.rank([_clip(1, ["beach", "sea"]), _clip(2, ["city", "traffic", "night"]), _clip(3, ["city"], dur=1), _clip(2, ["city", "traffic"])],
                    line, want="wide", min_seconds=4)
    assert [c["id"] for c in ranked] == ["2", "3", "1"]                      # the duplicate of 2 is dropped
    assert "Matches city, traffic" in ranked[0]["reasons"] and "Shorter than the beat" in ranked[1]["reasons"]
    tall = R.rank([_clip(5, ["city"], w=1080, h=1920)], line, want="tall")
    assert tall[0]["shape"] == "tall" and "Fits the video's shape" in tall[0]["reasons"]


async def test_meaning_based_reranking_reorders_the_best_few_and_a_failure_keeps_the_order():
    ranked = R.rank([_clip(1, ["city", "traffic"]), _clip(2, ["skyline", "dusk"])], "Cars crawl along the avenue at rush hour", want="wide")
    vectors = {"Cars crawl along the avenue at rush hour": [1.0, 0.0], "city traffic": [0.0, 1.0], "skyline dusk": [0.9, 0.1]}

    async def embed(text):
        return vectors[text]

    out, used = await R.rerank_by_meaning(ranked, "Cars crawl along the avenue at rush hour", embed)
    assert used is True and out[0]["id"] == "2"

    async def broken(text):
        raise RuntimeError("no embeddings")

    same, used = await R.rerank_by_meaning(ranked, "x", broken)
    assert used is False and [c["id"] for c in same] == [c["id"] for c in ranked]


def _words(text, start=0, per=300):
    out, t = [], start
    for w in text.split():
        out.append({"w": w, "s_ms": t, "e_ms": t + per - 30})
        t += per
    return out


def test_the_rail_picks_concrete_sentences_spaced_apart_with_queries_from_their_rarest_words():
    s1 = "Welcome back everyone to the channel today folks."
    s2 = "Morning traffic crawls across the old harbour bridge every single day."
    s3 = "Thanks for being here with me today everyone."
    s4 = "Fishermen unload crates of fresh mackerel beside the harbour market."
    words, t = [], 0
    for s in (s1, s2, s3, s4):
        ws = _words(s, t)
        words.extend(ws)
        t = ws[-1]["e_ms"] + 9000
    beats = R.rail(words, spacing_s=8, limit=5)
    lines = [b["line"] for b in beats]
    assert s2 in lines and s4 in lines
    assert beats == sorted(beats, key=lambda b: b["start_ms"])
    b2 = next(b for b in beats if b["line"] == s2)
    assert set(b2["keywords"]) <= set(R.keywords(s2)) and len(b2["keywords"]) <= 3 and b2["query"]
    assert R.rail([], limit=3) == [] and R.rail(_words("too few words"), limit=3) == []


# ── using a clip ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def storage(monkeypatch):
    saved = []

    async def upload(data, content_type, user_id, filename=None):
        saved.append((len(data), filename))
        return {"url": f"https://res.cloudinary.com/democloud/video/upload/v1/{uuid4().hex}.mp4", "width": 1280, "height": 720, "duration_s": 12.0,
                "bytes": len(data), "format": "mp4"}

    monkeypatch.setattr(S, "upload_file_detailed", upload)
    return saved


async def test_a_clip_is_saved_with_its_licence_and_author_and_using_it_again_returns_the_same_item(storage):
    ws = f"ws-{uuid4()}"

    async def get_one(provider, clip_id):
        return P.normalise_pixabay(PIXABAY_HIT)

    async def fetch(url):
        assert url.endswith("/m.mp4")
        return b"x" * 5000

    first = await S.use_clip(workspace_id=ws, user_id="u1", provider="pixabay", clip_id="125", query="city traffic", get_one=get_one, fetch=fetch)
    again = await S.use_clip(workspace_id=ws, user_id="u1", provider="pixabay", clip_id="125", get_one=get_one, fetch=fetch)
    assert first.id == again.id and len(storage) == 1
    assert first.origin == "stock" and first.contains_stock is True and first.contains_ai is False and first.source == MediaSource.STOCK
    assert first.stock["license"] == "Pixabay Content License" and first.stock["author"] == "Free-Videos" and first.stock["query"] == "city traffic"
    assert S.credit_line(first.model_dump()) == "Footage: Free-Videos via Pixabay"
    assert S.credit_line({"source": "uploaded"}) is None

    async def gone(provider, clip_id):
        return None

    with pytest.raises(Exception) as exc:
        await S.use_clip(workspace_id=ws, user_id="u1", provider="pixabay", clip_id="999", get_one=gone, fetch=fetch)
    assert "no longer available" in str(exc.value.detail)


async def test_a_download_larger_than_the_limit_is_refused_before_it_is_kept():
    class Res:
        status_code = 200
        headers = {"content-length": str(200 * 1024 * 1024)}

        async def aiter_bytes(self):
            yield b""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url):
            return Res()

    import app.video.stock.service as svc

    orig = svc.httpx.AsyncClient
    svc.httpx.AsyncClient = lambda **k: Client()
    try:
        with pytest.raises(Exception) as exc:
            await S.download("https://cdn.example/x.mp4")
        assert exc.value.status_code == 413
    finally:
        svc.httpx.AsyncClient = orig


def test_a_cutaway_is_placed_over_the_main_picture_with_its_own_sound_off_and_never_past_its_length():
    edit = edl.blank("landscape")
    edit["video"] = [{"id": "main", "media_id": "m1", "at_f": 0, "dur_f": 900, "src_in_f": 0, "speed": 1.0, "fit": "cover", "fade_in_f": 0,
                      "fade_out_f": 0, "audio": True, "gain_db": 0.0, "motion": False}]
    media = {"id": "stock1", "duration_s": 5.0, "kind": "video"}
    out, clip = S.insert_clip(edit, media, at_ms=10000, duration_ms=8000)
    assert clip["at_f"] == 300 and clip["dur_f"] == 150 and clip["audio"] is False                      # 5 s clip, 8 s asked for
    assert out["video"][-1]["id"] == clip["id"] and len(out["video"]) == 2 and edl.validate(out)
    out2, clip2 = S.insert_clip(edit, media, at_ms=29000, duration_ms=None)
    assert clip2["at_f"] + clip2["dur_f"] <= 900                                                           # kept inside the project
    still, c3 = S.insert_clip(edit, {"id": "pic", "duration_s": None, "kind": "image"}, at_ms=0, duration_ms=3000)
    assert c3["motion"] is True and c3["dur_f"] == 90


# ── the API ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _h(ws):
    return {"X-Workspace-Id": ws}


async def _setup(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Stock")
    return client, ws


async def _transcribed(ws):
    mid = str(uuid4())
    await media_assets.insert_one(MediaAsset(id=mid, workspace_id=ws, kind=MediaKind.VIDEO, url="https://x/v.mp4", mime_type="video/mp4", duration_s=60.0,
                                             source=MediaSource.UPLOADED, created_by="u", created_at=datetime.now(timezone.utc)).model_dump())
    words, t = [], 0
    for s in ("Welcome back everyone to the channel today folks.", "Morning traffic crawls across the old harbour bridge every single day.",
              "Fishermen unload crates of fresh mackerel beside the harbour market."):
        ws_ = _words(s, t)
        words.extend(ws_)
        t = ws_[-1]["e_ms"] + 9000
    tid = str(uuid4())
    await get_db()["video_transcripts"].insert_one({"id": tid, "workspace_id": ws, "media_id": mid, "status": "done", "language": "en", "duration_s": 60.0})
    await get_db()["video_transcript_pages"].insert_one({"transcript_id": tid, "page": 0, "words": words})
    return mid


async def test_search_and_the_rail_report_what_is_connected_and_what_the_video_says(signup_user, monkeypatch):
    client, ws = await _setup(signup_user)

    async def fake_get(provider, url, *, params=None, headers=None, attempts=3):
        return {"hits": [PIXABAY_HIT]} if provider == "pixabay" else {"videos": [PEXELS_VIDEO]}

    monkeypatch.setattr(P, "_get", fake_get)
    st = (await client.get("/api/v1/video-stock/status", headers=_h(ws))).json()
    assert st["ready"] is True and set(st["connected"]) == {"pixabay", "pexels"}
    res = (await client.get("/api/v1/video-stock/search?q=city traffic", headers=_h(ws))).json()
    assert len(res["clips"]) == 2 and res["clips"][0]["license"] and res["clips"][0]["author"] and "download_url" not in res["clips"][0]

    mid = await _transcribed(ws)
    rail = (await client.get(f"/api/v1/video-stock/rail/{mid}", headers=_h(ws))).json()
    assert rail["needs_translation"] is False and any("traffic" in b["query"] or "fishermen" in b["query"] for b in rail["beats"])
    idx = [b["index"] for b in rail["beats"]]
    opts = await client.post("/api/v1/video-stock/rail/options", json={"media_id": mid, "indexes": idx[:1]}, headers=_h(ws))
    assert opts.status_code == 200, opts.text
    beat = opts.json()["beats"][0]
    assert beat["options"] and beat["fallback_pictures"] is None and beat["query_used"]
    assert (await client.post("/api/v1/video-stock/rail/options", json={"media_id": mid, "indexes": [99]}, headers=_h(ws))).status_code == 404


async def test_when_nothing_is_connected_the_options_fall_back_to_the_brands_own_pictures(signup_user, monkeypatch):
    client, ws = await _setup(signup_user)
    monkeypatch.setattr(settings, "PIXABAY_API_KEY", "")
    monkeypatch.setattr(settings, "PEXELS_API_KEY", "", raising=False)
    await media_assets.insert_one(MediaAsset(id=str(uuid4()), workspace_id=ws, kind=MediaKind.IMAGE, url="https://x/logo.png", mime_type="image/png",
                                             source=MediaSource.BRAND_ASSET, created_by="u", created_at=datetime.now(timezone.utc)).model_dump())
    mid = await _transcribed(ws)
    st = (await client.get("/api/v1/video-stock/status", headers=_h(ws))).json()
    assert st["ready"] is False and "not available" in st["note"]
    rail = (await client.get(f"/api/v1/video-stock/rail/{mid}", headers=_h(ws))).json()
    opts = (await client.post("/api/v1/video-stock/rail/options", json={"media_id": mid, "indexes": [rail["beats"][0]["index"]]}, headers=_h(ws))).json()
    beat = opts["beats"][0]
    assert beat["options"] == [] and len(beat["fallback_pictures"]) == 1 and beat["errors"]


async def test_using_a_clip_saves_it_and_places_it_in_a_project_as_a_new_version(signup_user, monkeypatch, storage):
    client, ws = await _setup(signup_user)

    async def get_one(provider, clip_id):
        return P.normalise_pixabay(PIXABAY_HIT)

    async def fetch(url):
        return b"x" * 3000

    monkeypatch.setattr(P, "get_one", get_one)
    monkeypatch.setattr(S, "download", fetch)
    src = str(uuid4())
    await media_assets.insert_one(MediaAsset(id=src, workspace_id=ws, kind=MediaKind.VIDEO, url="https://res.cloudinary.com/democloud/video/upload/v1/m.mp4",
                                             mime_type="video/mp4", duration_s=30.0, width=1280, height=720, source=MediaSource.UPLOADED,
                                             created_by="u", created_at=datetime.now(timezone.utc)).model_dump())
    created = await client.post("/api/v1/video-projects", json={"title": "With b-roll", "source": "video", "media_id": src}, headers=_h(ws))
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]
    before = created.json()["version"]
    res = await client.post("/api/v1/video-stock/use", json={"provider": "pixabay", "clip_id": "125", "query": "city", "project_id": project_id,
                                                              "at_ms": 5000, "duration_ms": 4000}, headers=_h(ws))
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["media"]["origin"] == "stock" and body["media"]["credit"] == "Footage: Free-Videos via Pixabay"
    assert body["clip"]["audio"] is False and body["clip"]["dur_f"] == 120 and body["project"]["version"] == before + 1
    credits = (await client.get(f"/api/v1/video-stock/credits/{project_id}", headers=_h(ws))).json()
    assert credits["text"] == "Footage: Free-Videos via Pixabay" and credits["credits"][0]["license"] == "Pixabay Content License"
    versions = (await client.get(f"/api/v1/video-projects/{project_id}/versions", headers=_h(ws))).json()["versions"]
    assert any(v["label"] == "Added stock footage" for v in versions)


async def test_stock_use_checks_the_clip_and_the_project_and_stays_inside_the_workspace(signup_user, monkeypatch, storage):
    client, ws = await _setup(signup_user)

    async def gone(provider, clip_id):
        return None

    monkeypatch.setattr(P, "get_one", gone)
    missing = await client.post("/api/v1/video-stock/use", json={"provider": "pixabay", "clip_id": "1"}, headers=_h(ws))
    assert missing.status_code == 404
    bad = await client.post("/api/v1/video-stock/use", json={"provider": "other", "clip_id": "1"}, headers=_h(ws))
    assert bad.status_code == 422
    other, _ = await signup_user()
    other_ws = await create_workspace(other, "Elsewhere")
    mid = await _transcribed(ws)
    assert (await other.get(f"/api/v1/video-stock/rail/{mid}", headers=_h(other_ws))).status_code == 404
