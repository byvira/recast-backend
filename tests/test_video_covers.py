"""Covers and thumbnails: choosing frames, ranking them, cutting them to each platform without cutting a face, and building the
cover as a picture the Image editor opens. Real database and real picture drawing; only storage upload and the model are replaced."""
import io
import random
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from PIL import Image

from app.api.v1 import image_assets as image_module
from app.api.v1 import video_covers as covers_api
from app.core.config import settings
from app.db.mongo import get_db, image_assets, media_assets
from app.models.media import MediaAsset, MediaKind, MediaSource
from app.video import jobs
from app.video.analysis import analyze as A
from app.video.covers import crop as C
from app.video.covers import platforms as P
from app.video.covers import rank as R
from app.video.covers import text as T
from tests.conftest import create_workspace


@pytest.fixture(autouse=True)
def _cloud(monkeypatch):
    monkeypatch.setattr(settings, "CLOUDINARY_CLOUD_NAME", "democloud")
    monkeypatch.setattr(settings, "CLOUDINARY_API_KEY", "1234")
    monkeypatch.setattr(settings, "CLOUDINARY_API_SECRET", "shhh")
    monkeypatch.setattr(settings, "VIDEO_WORKER_ENABLED", True)


@pytest.fixture(autouse=True)
async def _clean():
    await get_db()["video_jobs"].delete_many({})
    await get_db()["video_cover_frames"].delete_many({})
    yield


# ── the platform table ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_platform_table_has_each_platforms_own_size_and_a_file_limit():
    assert (P.PLATFORMS["youtube"]["w"], P.PLATFORMS["youtube"]["h"]) == (1280, 720)
    assert (P.PLATFORMS["youtube_shorts"]["w"], P.PLATFORMS["youtube_shorts"]["h"]) == (1080, 1920)
    assert (P.PLATFORMS["podcast"]["w"], P.PLATFORMS["podcast"]["h"]) == (3000, 3000)
    assert (P.PLATFORMS["instagram_feed"]["w"], P.PLATFORMS["instagram_feed"]["h"]) == (1080, 1350)
    for row in P.PLATFORMS.values():
        assert row["max_bytes"] > 0 and row["max_lines"] >= 2 and row["max_chars"] >= 20
        left, top, right, bottom = row["safe"]
        assert 0 < left < 0.3 and 0 < top < 0.3 and 0 < right < 0.3 and 0 < bottom < 0.4
    assert {t["orientation"] for t in P.public_table()} == {"wide", "tall", "square"}


# ── which moments to take pictures of ────────────────────────────────────────────────────────────────────────────────────

def test_candidate_moments_are_spaced_inside_the_video_and_include_each_shot_and_each_clip_worthy_moment():
    scenes = [{"t": 10.0, "score": 0.5}, {"t": 30.0, "score": 0.6}, {"t": 30.5, "score": 0.4}]
    moments = [{"start_ms": 45000, "state": "suggested"}, {"start_ms": 12000, "state": "rejected"}]
    times = R.candidate_times(60.0, scenes, moments)
    assert times == sorted(times) and all(0.5 <= t <= 59.5 for t in times)
    assert all(b - a >= R.MIN_GAP_S for a, b in zip(times, times[1:]))
    assert any(abs(t - 5.0) < 0.1 for t in times)                 # the middle of the first shot
    assert any(abs(t - 46.0) < 0.1 for t in times)                # a second after the clip-worthy moment starts
    assert not any(abs(t - 13.0) < 0.1 for t in times)            # a rejected moment is not a reason to take a picture
    assert R.candidate_times(0, [], []) == []
    assert len(R.candidate_times(7200.0, [{"t": float(i * 10)} for i in range(1, 700)], [], limit=24)) == 24


# ── ranking ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _frame(fid, t, **kw):
    base = {"id": fid, "t": t, "url": f"https://x/{fid}.jpg", "sharpness": 0.1, "brightness": 0.5, "contrast": 0.3,
            "top_busy": 0.05, "bottom_busy": 0.03, "hash": f"{random.Random(fid).getrandbits(64):016x}", "faces": None}
    base.update(kw)
    return base


def test_a_sharp_well_lit_frame_with_room_for_words_beats_a_soft_dark_busy_one():
    good = _frame("good", 10, sharpness=0.14, brightness=0.55, bottom_busy=0.02)
    soft = _frame("soft", 20, sharpness=0.02, brightness=0.12, bottom_busy=0.2)
    ranked = R.rank_frames([soft, good])
    assert [f["id"] for f in ranked] == ["good", "soft"]
    assert ranked[0]["score"] > ranked[1]["score"] + 20
    assert "Sharp and detailed" in ranked[0]["reasons"] and "Clear space for words" in ranked[0]["reasons"]
    assert "Quite dark" in ranked[1]["notes"]


def test_a_nearly_black_frame_is_never_a_candidate_and_a_near_copy_is_listed_last_with_what_it_copies():
    black = _frame("black", 1, brightness=0.02)
    a = _frame("a", 5, sharpness=0.13, hash="ffff0000ffff0000")
    near = _frame("near", 6, sharpness=0.1, hash="ffff0000ffff0001")           # one bit apart
    other = _frame("other", 9, sharpness=0.06, hash="00ff00ff00ff00ff")
    ranked = R.rank_frames([black, near, a, other])
    assert [f["id"] for f in ranked] == ["a", "other", "near"]
    assert ranked[2]["duplicate_of"] == "a" and ranked[0]["duplicate_of"] is None
    assert [f["id"] for f in R.pick_distinct(ranked, 3)] == ["a", "other"]


def test_a_face_of_a_good_size_adds_to_the_score_and_not_checking_faces_is_not_the_same_as_no_faces():
    face = {"x": 0.4, "y": 0.2, "w": 0.28, "h": 0.28}
    with_face = R.score_frame(_frame("a", 1, faces=[face]))
    no_face = R.score_frame(_frame("b", 1, faces=[]))
    unchecked = R.score_frame(_frame("c", 1, faces=None))
    assert with_face["score"] > no_face["score"] and with_face["face_count"] == 1 and "A face you can see" in with_face["reasons"]
    assert unchecked["face_count"] is None and unchecked["faces_checked"] is False and no_face["faces_checked"] is True


def test_a_frame_in_the_brand_colour_family_gets_a_small_lift():
    base = _frame("a", 1, rgb=[220, 60, 30])
    off = _frame("b", 1, rgb=[30, 60, 220])
    assert R.score_frame(base, brand_hex="#e04a1f")["score"] >= R.score_frame(off, brand_hex="#e04a1f")["score"]
    assert "Matches the brand colour" in R.score_frame(base, brand_hex="#e04a1f")["reasons"]


# ── cutting to a shape without cutting a face ───────────────────────────────────────────────────────────────────────────

def test_the_crop_never_cuts_through_a_face_whatever_the_shape_or_position():
    rng = random.Random(7)
    shapes = [(1080, 1920), (1080, 1350), (1080, 1080), (1280, 720), (1200, 630), (1000, 1500), (3000, 3000)]
    for _ in range(400):
        w = rng.choice([0.06, 0.1, 0.18, 0.3])
        h = w * rng.uniform(1.0, 1.5)
        face = {"x": rng.uniform(0, 1 - w), "y": rng.uniform(0, 1 - h), "w": w, "h": h}
        dst = rng.choice(shapes)
        src = rng.choice([(1280, 720), (1920, 1080), (720, 1280), (1000, 1000)])
        box, mode = C.crop_box(src[0], src[1], dst[0], dst[1], [face])
        assert 0 <= box[0] < box[2] <= src[0] and 0 <= box[1] < box[3] <= src[1]
        if mode == "faces":
            fx0, fx1 = face["x"] * src[0], (face["x"] + face["w"]) * src[0]
            fy0, fy1 = face["y"] * src[1], (face["y"] + face["h"]) * src[1]
            assert box[0] <= fx0 + 1 and box[2] >= fx1 - 1 and box[1] <= fy0 + 1 and box[3] >= fy1 - 1
            assert abs((box[2] - box[0]) / (box[3] - box[1]) - dst[0] / dst[1]) < 0.02
        else:
            assert mode == "blur"


def test_a_frame_is_cut_to_the_exact_platform_size_and_a_wide_face_group_keeps_the_whole_frame_on_a_soft_backdrop():
    src = Image.new("RGB", (1280, 720), (200, 40, 40))
    buf = io.BytesIO()
    src.save(buf, "JPEG")
    out, mode, _ = C.fit_frame(buf.getvalue(), 1080, 1920, [{"x": 0.45, "y": 0.2, "w": 0.1, "h": 0.2}])
    assert Image.open(io.BytesIO(out)).size == (1080, 1920) and mode == "faces"
    out, mode, _ = C.fit_frame(buf.getvalue(), 1080, 1920, [{"x": 0.05, "y": 0.1, "w": 0.9, "h": 0.3}])
    assert Image.open(io.BytesIO(out)).size == (1080, 1920) and mode == "blur"
    out, mode, _ = C.fit_frame(buf.getvalue(), 1200, 630, None)
    assert Image.open(io.BytesIO(out)).size == (1200, 630) and mode == "centre"


def test_a_picture_is_saved_smaller_to_fit_a_platforms_limit_and_a_limit_that_cannot_be_met_is_refused():
    rng = random.Random(3)
    noisy = Image.new("RGB", (1280, 720))
    noisy.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(1280 * 720)])
    buf = io.BytesIO()
    noisy.save(buf, "PNG")
    big = buf.getvalue()
    assert len(big) > 1_500_000
    out, mime = C.under_limit(big, 1_000_000)
    assert mime == "image/jpeg" and len(out) <= 1_000_000
    same, mime = C.under_limit(big, len(big) + 10)
    assert same == big and mime == "image/png"
    with pytest.raises(ValueError):
        C.under_limit(big, 500)


# ── the words ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_cover_words_are_cut_at_a_word_deduplicated_and_the_accent_must_be_in_the_line():
    assert T.fallback("Why most weekly plans quietly fall apart by Wednesday", 30) == "Why most weekly plans quietly"
    assert T.fallback("Short", 30) == "Short"
    raw = {"options": [
        {"text": "\"Plans fall apart by Wednesday\"", "accent": "Wednesday"},
        {"text": "plans fall apart by wednesday", "accent": ""},                       # the same line again
        {"text": "Stop planning the whole week", "accent": "not in the line"},
        {"text": "x", "accent": ""},
    ]}
    got = T.validate_options(raw, 42, 3, 3)
    assert [g["text"] for g in got] == ["Plans fall apart by Wednesday", "Stop planning the whole week"]
    assert got[0]["accent"] == "Wednesday" and got[1]["accent"] == ""
    assert T.validate_options({"options": [{"text": "word " * 40, "accent": ""}]}, 500, 3, 3) == []


async def test_cover_options_use_the_model_when_it_answers_well_and_the_title_when_it_does_not():
    async def good(prompt):
        assert "YouTube thumbnail" in prompt and "calmer weeks" in prompt
        return {"options": [{"text": "A calmer week starts Sunday", "accent": "calmer"}]}

    async def broken(prompt):
        raise RuntimeError("busy")

    options, by_model = await T.cover_options(platform_id="youtube", material="A talk about calmer weeks", title="Calmer weeks", caller=good)
    assert by_model is True and options[0]["text"] == "A calmer week starts Sunday"
    options, by_model = await T.cover_options(platform_id="youtube", material="A talk about calmer weeks, again", title="Calmer weeks", caller=broken)
    assert by_model is False and options == [{"text": "Calmer weeks", "accent": ""}]
    options, by_model = await T.cover_options(platform_id="youtube", material="", title="Only a title")
    assert by_model is False and options[0]["text"] == "Only a title"


# ── the frames job ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _frame_url(ws):
    return f"https://res.cloudinary.com/democloud/image/upload/v1/recast/video_frames/{ws}/{uuid4()}_frame_f.jpg"


async def _video(ws: str, *, preview: bool = True) -> dict:
    row = MediaAsset(
        id=str(uuid4()), workspace_id=ws, kind=MediaKind.VIDEO, url=f"https://res.cloudinary.com/democloud/video/upload/v1/{uuid4()}.mp4",
        mime_type="video/mp4", duration_s=60.0, source=MediaSource.UPLOADED, created_by="u1", created_at=datetime.now(timezone.utc),
    ).model_dump()
    if preview:
        row["preview_status"] = "ready"
        row["preview"] = {"proxy_url": f"https://res.cloudinary.com/democloud/video/upload/v1/recast/video_preview/{ws}/{uuid4()}_proxy",
                          "meta": {"duration_s": 60.0, "scenes": [{"t": 20.0, "score": 0.5}, {"t": 40.0, "score": 0.5}]}}
    await media_assets.insert_one(dict(row))
    return row


def _worker_answer(ws: str, n: int = 4, **over):
    outputs, rows = [], []
    for i in range(n):
        name = f"f{i:03d}.jpg"
        outputs.append({"role": "frame", "url": _frame_url(ws), "bytes": 5000 + i, "name": name})
        rows.append({"file": name, "index": i, "t": 5.0 + 10 * i, "sharpness": 0.05 + 0.03 * i, "brightness": 0.5, "contrast": 0.3,
                     "top_busy": 0.05, "bottom_busy": 0.03, "hash": f"{(1 << (9 * i + 1)) - 1:016x}", "rgb": [100, 120, 140],
                     "faces": [{"x": 0.4, "y": 0.2, "w": 0.2, "h": 0.25}] if i == 3 else []})
    report = {"kind": "frames", "passed": True, "faces_checked": True, "width": 1280, "height": 720, "frames": rows, **over}
    return outputs, report


@pytest.fixture
def stored(monkeypatch):
    async def size(url):
        return None

    async def remote(url):
        return int(sizes.get(url, 0)) or None

    sizes: dict[str, int] = {}
    monkeypatch.setattr(jobs, "_remote_size", remote)
    return sizes


async def test_a_frames_job_is_queued_once_handed_to_the_worker_and_replaced_after_a_failure():
    ws = f"ws-{uuid4()}"
    media = await _video(ws)
    url = media["preview"]["proxy_url"]
    first = await jobs.enqueue_frames(workspace_id=ws, user_id="u1", media_id=media["id"], url=url, times_s=[3, 1, 1, 7.123])
    again = await jobs.enqueue_frames(workspace_id=ws, user_id="u1", media_id=media["id"], url=url, times_s=[1, 3, 7.123])
    assert first["id"] == again["id"]
    claimed = await jobs.claim("w1", "0.3.0", {})
    assert claimed["kind"] == "frames" and claimed["frames"]["times_s"] == [1.0, 3.0, 7.12] and claimed["inputs"]["src"]["url"] == url
    await jobs.fail(claimed["id"], claimed["lease_token"], "render_failed", "no", False)
    third = await jobs.enqueue_frames(workspace_id=ws, user_id="u1", media_id=media["id"], url=url, times_s=[1, 3, 7.123])
    assert third["id"] != first["id"]
    with pytest.raises(jobs.JobError):
        await jobs.enqueue_frames(workspace_id=ws, user_id="u1", media_id=media["id"], url="http://insecure/x.mp4", times_s=[1])
    with pytest.raises(jobs.JobError):
        await jobs.enqueue_frames(workspace_id=ws, user_id="u1", media_id=media["id"], url=url, times_s=[])


async def test_the_frames_the_worker_reports_are_kept_with_their_measurements_after_every_file_is_checked(stored):
    ws = f"ws-{uuid4()}"
    media = await _video(ws)
    await jobs.enqueue_frames(workspace_id=ws, user_id="u1", media_id=media["id"], url=media["preview"]["proxy_url"], times_s=[5, 15, 25, 35])
    claimed = await jobs.claim("w1", "0.3.0", {})
    outputs, report = _worker_answer(ws)
    for o in outputs:
        stored[o["url"]] = o["bytes"]
    res = await jobs.complete(claimed["id"], claimed["lease_token"], outputs, report)
    assert res["accepted"] is True
    doc = await get_db()["video_cover_frames"].find_one({"workspace_id": ws, "media_id": media["id"]})
    assert len(doc["frames"]) == 4 and doc["faces_checked"] is True
    assert doc["frames"][3]["faces"] == [{"x": 0.4, "y": 0.2, "w": 0.2, "h": 0.25}] and doc["frames"][0]["faces"] == []
    assert doc["frames"][0]["id"] == "f1" and doc["frames"][0]["rgb"] == [100, 120, 140]


async def test_a_frames_report_with_a_wrong_size_a_foreign_address_or_a_missing_measurement_is_refused(stored):
    ws = f"ws-{uuid4()}"
    media = await _video(ws)

    async def attempt(mutate):
        await jobs.enqueue_frames(workspace_id=ws, user_id="u1", media_id=media["id"], url=media["preview"]["proxy_url"], times_s=[5, 15, 25, 35])
        claimed = await jobs.claim("w1", "0.3.0", {})
        outputs, report = _worker_answer(ws)
        for o in outputs:
            stored[o["url"]] = o["bytes"]
        mutate(outputs, report, stored)
        return await jobs.complete(claimed["id"], claimed["lease_token"], outputs, report)

    def wrong_size(outputs, report, sizes):
        sizes[outputs[1]["url"]] = 1

    def foreign(outputs, report, sizes):
        outputs[0]["url"] = "https://evil.example/x.jpg"

    def no_measure(outputs, report, sizes):
        del report["frames"][2]["sharpness"]

    for mutate in (wrong_size, foreign, no_measure):
        res = await attempt(mutate)
        assert res["accepted"] is False
        await get_db()["video_jobs"].delete_many({"workspace_id": ws})
    assert await get_db()["video_cover_frames"].find_one({"workspace_id": ws}) is None


# ── the API: ranking per platform, covers, and the Image editor ─────────────────────────────────────────────────────────

def _jpeg(size=(1280, 720)) -> bytes:
    im = Image.new("RGB", size)
    px = im.load()
    for x in range(0, size[0], 4):
        for y in range(0, size[1], 4):
            c = ((x * 255) // size[0], (y * 255) // size[1], 90)
            for dx in range(4):
                for dy in range(4):
                    if x + dx < size[0] and y + dy < size[1]:
                        px[x + dx, y + dy] = c
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return buf.getvalue()


@pytest.fixture
def pictures(monkeypatch):
    saved = []

    async def fake_upload(data, content_type, user_id):
        saved.append(data)
        return f"https://res.cloudinary.com/democloud/image/upload/v1/recast/{uuid4().hex}"

    async def fake_fetch(url):
        return _jpeg()

    async def fake_logo(url):
        return _jpeg()

    monkeypatch.setattr(image_module, "upload_file", fake_upload)
    monkeypatch.setattr(image_module, "_fetch_logo_bytes", fake_logo)
    monkeypatch.setattr(covers_api, "_fetch", fake_fetch)
    return saved


async def _setup(signup_user):
    client, profile = await signup_user()
    ws = await create_workspace(client, "Covers")
    res = await client.post("/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws})
    assert res.status_code in (200, 201), res.text
    return client, profile, ws, res.json()["brand_profile_id"]


async def _with_frames(ws, stored):
    media = await _video(ws)
    await jobs.enqueue_frames(workspace_id=ws, user_id="u1", media_id=media["id"], url=media["preview"]["proxy_url"], times_s=[5, 15, 25, 35])
    claimed = await jobs.claim("w1", "0.3.0", {})
    outputs, report = _worker_answer(ws)
    for o in outputs:
        stored[o["url"]] = o["bytes"]
    assert (await jobs.complete(claimed["id"], claimed["lease_token"], outputs, report))["accepted"]
    return media


def _h(ws):
    return {"X-Workspace-Id": ws}


async def test_the_state_goes_from_none_to_queued_to_done_with_a_ranked_list_and_the_platform_table_is_served(signup_user, stored):
    client, _, ws, _brand = await _setup(signup_user)
    table = (await client.get("/api/v1/video-covers/platforms", headers=_h(ws))).json()["platforms"]
    assert {t["id"] for t in table} == set(P.PLATFORMS)

    media = await _video(ws)
    first = (await client.get(f"/api/v1/video-covers/{media['id']}", headers=_h(ws))).json()
    assert first["status"] == "none" and first["frames"] == []
    started = await client.post(f"/api/v1/video-covers/{media['id']}/frames", headers=_h(ws))
    assert started.status_code == 202 and started.json()["times"] >= 4
    queued = (await client.get(f"/api/v1/video-covers/{media['id']}", headers=_h(ws))).json()
    assert queued["status"] == "queued"

    claimed = await jobs.claim("w1", "0.3.0", {})
    assert claimed["frames"]["times_s"] == sorted(claimed["frames"]["times_s"]) and len(claimed["frames"]["times_s"]) >= 4
    outputs, report = _worker_answer(ws)
    for o in outputs:
        stored[o["url"]] = o["bytes"]
    await jobs.complete(claimed["id"], claimed["lease_token"], outputs, report)
    done = (await client.get(f"/api/v1/video-covers/{media['id']}?platform=youtube_shorts", headers=_h(ws))).json()
    assert done["status"] == "done" and done["platform"] == "youtube_shorts" and len(done["frames"]) == 4
    scores = [f["score"] for f in done["frames"] if not f["duplicate_of"]]
    assert scores == sorted(scores, reverse=True)
    assert done["frames"][0]["id"] and done["frames"][0]["url"].startswith("https://")
    assert (await client.get(f"/api/v1/video-covers/{media['id']}?platform=nope", headers=_h(ws))).status_code == 400


async def test_pictures_cannot_be_chosen_before_the_preview_is_ready(signup_user):
    client, _, ws, _brand = await _setup(signup_user)
    media = await _video(ws, preview=False)
    res = await client.post(f"/api/v1/video-covers/{media['id']}/frames", headers=_h(ws))
    assert res.status_code == 409 and "preview" in res.json()["detail"].lower()
    assert (await client.post("/api/v1/video-covers/missing/frames", headers=_h(ws))).status_code == 404


async def test_three_covers_for_a_platform_are_the_exact_size_editable_and_carry_the_videos_origin(signup_user, stored, pictures, monkeypatch):
    client, _, ws, brand_id = await _setup(signup_user)
    media = await _with_frames(ws, stored)
    await media_assets.update_one({"id": media["id"]}, {"$set": {"origin": "ai", "contains_ai": True}})
    await get_db()["video_analysis"].insert_one({
        "id": str(uuid4()), "workspace_id": ws, "media_id": media["id"], "status": "done",
        "outline": {"title": "Calmer weeks", "summary": "A talk about planning a calmer week.", "topics": ["planning"]}})

    async def model(prompt):
        return {"options": [{"text": "A calmer week starts Sunday", "accent": "calmer"}, {"text": "Plan less, finish more", "accent": "finish"},
                            {"text": "Wednesday is where plans break", "accent": "Wednesday"}]}

    monkeypatch.setattr(A, "_default_caller", model)
    res = await client.post(f"/api/v1/video-covers/{media['id']}/covers", json={"platform": "youtube", "brand_id": brand_id, "count": 3},
                            headers=_h(ws))
    assert res.status_code == 201, res.text
    covers = res.json()["covers"]
    assert len(covers) == 3 and len({c["frame_id"] for c in covers}) == 3
    for c in covers:
        assert (c["width"], c["height"]) == (1280, 720) and c["bytes"] <= P.PLATFORMS["youtube"]["max_bytes"]
        assert c["origin"] == "ai" and c["contains_ai"] is True and c["text"]
        item = await media_assets.find_one({"id": c["media_id"]})
        assert (item["width"], item["height"]) == (1280, 720) and item["origin"] == "ai"
    assert len({c["text"] for c in covers}) == 3

    # the cover is an ordinary picture project the Image editor opens, with its layers and a clean backdrop
    asset = (await client.get(f"/api/v1/image-assets/{covers[0]['image_asset_id']}", headers=_h(ws))).json()
    slide = asset["slides"][0]
    assert (slide["width"], slide["height"]) == (1280, 720) and slide["background_media_id"] and slide["media_id"] == covers[0]["media_id"]
    assert {l["type"] for l in slide["layers"]} >= {"text", "shape"}
    headline = next(l for l in slide["layers"] if l["type"] == "text")
    assert headline["text"] == covers[0]["text"]
    stored_doc = await image_assets.find_one({"id": covers[0]["image_asset_id"]})
    assert stored_doc["cover_for"]["platform"] == "youtube" and stored_doc["cover_for"]["media_id"] == media["id"]

    # editing the words in the image editor redraws the same cover
    layers = slide["layers"]
    for l in layers:
        if l["id"] == headline["id"]:
            l["text"] = "A new line"
    saved = await client.put(f"/api/v1/image-assets/{asset['id']}/slides/1/layers", json={"layers": layers}, headers=_h(ws))
    assert saved.status_code == 200, saved.text
    assert saved.json()["slides"][0]["media_id"] != covers[0]["media_id"]


async def test_a_tall_platform_gets_a_tall_cover_and_chosen_pictures_and_words_are_respected(signup_user, stored, pictures, monkeypatch):
    client, _, ws, brand_id = await _setup(signup_user)
    media = await _with_frames(ws, stored)

    async def model(prompt):
        raise RuntimeError("model is busy")

    monkeypatch.setattr(A, "_default_caller", model)
    doc = await get_db()["video_cover_frames"].find_one({"workspace_id": ws, "media_id": media["id"]})
    pick = doc["frames"][3]["id"]
    res = await client.post(f"/api/v1/video-covers/{media['id']}/covers",
                            json={"platform": "instagram_reels", "brand_id": brand_id, "frame_ids": [pick], "text": "My own words"}, headers=_h(ws))
    assert res.status_code == 201, res.text
    only = res.json()["covers"]
    assert len(only) == 1 and (only[0]["width"], only[0]["height"]) == (1080, 1920) and only[0]["frame_id"] == pick and only[0]["text"] == "My own words"
    assert only[0]["origin"] == "own"
    missing = await client.post(f"/api/v1/video-covers/{media['id']}/covers",
                               json={"platform": "youtube", "brand_id": brand_id, "frame_ids": ["f99"]}, headers=_h(ws))
    assert missing.status_code == 404
    nothing = await _video(ws)
    none_yet = await client.post(f"/api/v1/video-covers/{nothing['id']}/covers", json={"platform": "youtube", "brand_id": brand_id}, headers=_h(ws))
    assert none_yet.status_code == 409


async def test_covers_for_a_podcast_or_a_post_come_from_the_brand_and_a_library_picture_is_cut_to_shape(signup_user, pictures, monkeypatch):
    client, _, ws, brand_id = await _setup(signup_user)

    async def model(prompt):
        return {"options": [{"text": "Episode 12: why weeks go wrong", "accent": "weeks"}]}

    monkeypatch.setattr(A, "_default_caller", model)
    res = await client.post("/api/v1/video-covers/compose", json={
        "platform": "podcast", "brand_id": brand_id, "title": "Why weeks go wrong", "material": "An episode about planning.", "count": 3,
    }, headers=_h(ws))
    assert res.status_code == 201, res.text
    covers = res.json()["covers"]
    assert len(covers) == 3 and all((c["width"], c["height"]) == (3000, 3000) for c in covers)
    assert all(c["origin"] == "template" and c["bytes"] <= P.PLATFORMS["podcast"]["max_bytes"] for c in covers)

    pic = MediaAsset(
        id=str(uuid4()), workspace_id=ws, kind=MediaKind.IMAGE, url="https://res.cloudinary.com/democloud/image/upload/v1/p.jpg",
        mime_type="image/jpeg", source=MediaSource.UPLOADED, created_by="u1", created_at=datetime.now(timezone.utc),
    ).model_dump()
    await media_assets.insert_one(dict(pic))
    res = await client.post("/api/v1/video-covers/compose", json={
        "platform": "linkedin", "brand_id": brand_id, "title": "Why weeks go wrong", "picture_media_id": pic["id"],
    }, headers=_h(ws))
    assert res.status_code == 201, res.text
    one = res.json()["covers"]
    assert len(one) == 1 and (one[0]["width"], one[0]["height"]) == (1200, 627) and one[0]["origin"] == "own"
    bad = await client.post("/api/v1/video-covers/compose", json={
        "platform": "linkedin", "brand_id": brand_id, "title": "x", "picture_media_id": "nope"}, headers=_h(ws))
    assert bad.status_code == 404


async def test_a_member_in_another_workspace_cannot_read_or_use_the_frames(signup_user, stored):
    client, _, ws, brand_id = await _setup(signup_user)
    media = await _with_frames(ws, stored)
    other_client, _ = await signup_user()
    other_ws = await create_workspace(other_client, "Elsewhere")
    assert (await other_client.get(f"/api/v1/video-covers/{media['id']}", headers=_h(other_ws))).status_code == 404
    assert (await other_client.post(f"/api/v1/video-covers/{media['id']}/covers", json={"platform": "youtube"}, headers=_h(other_ws))).status_code == 404


async def test_cover_words_are_written_from_the_opening_of_the_transcript_when_the_video_has_not_been_read(signup_user, stored, pictures, monkeypatch):
    client, _, ws, brand_id = await _setup(signup_user)
    media = await _with_frames(ws, stored)
    tid = str(uuid4())
    await get_db()["video_transcripts"].insert_one({"id": tid, "workspace_id": ws, "media_id": media["id"], "status": "done", "language": "en"})
    await get_db()["video_transcript_pages"].insert_one({"transcript_id": tid, "page": 0, "words": [
        {"w": w, "s_ms": i * 300, "e_ms": i * 300 + 250} for i, w in enumerate("today we talk about why weekly plans fall apart".split())]})
    seen = []

    async def model(prompt):
        seen.append(prompt)
        return {"options": [{"text": "Why weekly plans fall apart", "accent": "weekly"}]}

    monkeypatch.setattr(A, "_default_caller", model)
    res = await client.post(f"/api/v1/video-covers/{media['id']}/covers", json={"platform": "x", "brand_id": brand_id, "count": 1}, headers=_h(ws))
    assert res.status_code == 201, res.text
    assert res.json()["covers"][0]["text"] == "Why weekly plans fall apart" and res.json()["covers"][0]["words_by_model"] is True
    assert seen and "weekly plans fall apart" in seen[0]
