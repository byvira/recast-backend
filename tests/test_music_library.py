"""Tests for the curated music library (app.pipelines.media.music_library)
and its merge into the Music tab's bed picker (app.api.v1.audio_assets).

Real Mongo; the only stubs are Jamendo's own HTTP calls and the Cloudinary
upload, same pattern as test_video_understanding.py's _FakeHttp. Every
Jamendo track is CC BY (attribution required) at minimum — live-confirmed
against Jamendo's real API and docs, no CC0 filter exists — so these tests
assert the real, dynamic license name, not a hardcoded "CC0".
"""

from uuid import uuid4

import pytest

from app.api.v1 import audio_assets as audio_module
from app.core.config import settings
from app.db.mongo import media_assets, music_library_tracks
from app.models.audio_asset import AudioKit, MusicBed
from app.pipelines.media import music_library as lib_module
from app.pipelines.media.music_library import MusicLibraryError, fetch_curated_tracks, list_library_tracks
from tests.conftest import create_workspace
from tests.test_audio_assets import _brand, _h, stubs  # noqa: F401 — fixture reuse

_CC_BY_URL = "https://creativecommons.org/licenses/by/3.0/"


class _FakeSearchResponse:
    def __init__(self, tracks):
        self._tracks = tracks

    def raise_for_status(self):
        return None

    def json(self):
        return {"results": self._tracks}


class _FakeDownloadResponse:
    def __init__(self, payload: bytes):
        self.content = payload

    def raise_for_status(self):
        return None


class _FakeHttp:
    """Stands in for httpx.AsyncClient inside music_library — the first
    call (Jamendo search) returns JSON, every call after is a track download."""

    def __init__(self, tracks, payload=b"mp3-bytes"):
        self.tracks, self.payload, self.calls = tracks, payload, []

    def __call__(self, *a, **kw):
        outer = self

        class _Client:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            async def get(self_inner, url, **k):
                outer.calls.append(url)
                if url == lib_module.JAMENDO_TRACKS_URL:
                    return _FakeSearchResponse(outer.tracks)
                return _FakeDownloadResponse(outer.payload)

        return _Client()


class _FakePagedHttp:
    """Like _FakeHttp, but returns a different page per `offset` param —
    for real pagination coverage, including a page that comes back empty
    (Jamendo's own live flakiness) before a later page succeeds."""

    def __init__(self, pages_by_offset: dict, payload=b"mp3-bytes"):
        self.pages_by_offset, self.payload, self.search_calls = pages_by_offset, payload, []

    def __call__(self, *a, **kw):
        outer = self

        class _Client:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            async def get(self_inner, url, *, params=None, **k):
                if url == lib_module.JAMENDO_TRACKS_URL:
                    offset = (params or {}).get("offset", 0)
                    outer.search_calls.append(offset)
                    return _FakeSearchResponse(outer.pages_by_offset.get(offset, []))
                return _FakeDownloadResponse(outer.payload)

        return _Client()


def _jamendo_tracks(n=2, license_ccurl=_CC_BY_URL):
    # Real ids, not fixed literals — the test DB is only dropped once per
    # session (see conftest.py's _test_database_lifecycle), so a fixed id
    # would collide with whatever an earlier test in this file already
    # ingested and be silently treated as "already there."
    stamp = uuid4().hex
    return [
        {
            "id": f"{stamp}-{i}",
            "name": f"Track {i}",
            "artist_name": f"Artist {i}",
            "audiodownload": f"https://example.com/{i}.mp3",
            "license_ccurl": license_ccurl,
        }
        for i in range(n)
    ]


async def _stub_upload(monkeypatch, url="https://res.cloudinary.com/demo/lib.mp3"):
    async def _fake(data, content_type, user_id):
        return {"url": url, "bytes": len(data), "duration_s": 30.0, "width": None, "height": None, "format": "mp3"}

    monkeypatch.setattr(lib_module, "upload_file_detailed", _fake)


# ── fetch_curated_tracks ─────────────────────────────────────────────────────

async def test_refuses_to_run_without_a_client_id(monkeypatch):
    monkeypatch.setattr(settings, "JAMENDO_CLIENT_ID", "")
    with pytest.raises(MusicLibraryError, match="JAMENDO_CLIENT_ID"):
        await fetch_curated_tracks()


async def test_ingests_real_tracks_and_records_the_real_cc_by_license(monkeypatch):
    monkeypatch.setattr(settings, "JAMENDO_CLIENT_ID", "test-client")
    monkeypatch.setattr(lib_module.httpx, "AsyncClient", _FakeHttp(_jamendo_tracks(2)))
    await _stub_upload(monkeypatch)

    tracks = await fetch_curated_tracks(limit=2)
    assert len(tracks) == 2
    assert tracks[0].name == "Track 0"
    assert tracks[0].artist == "Artist 0"
    assert tracks[0].license_name == "CC BY 3.0"
    assert tracks[0].license_url == _CC_BY_URL
    assert tracks[0].source == "jamendo"

    media = await media_assets.find_one({"id": tracks[0].media_id})
    assert media["source"] == "library"
    assert media["kind"] == "audio"


async def test_a_track_with_no_license_url_still_gets_a_real_cc_by_default(monkeypatch):
    monkeypatch.setattr(settings, "JAMENDO_CLIENT_ID", "test-client")
    monkeypatch.setattr(lib_module.httpx, "AsyncClient", _FakeHttp(_jamendo_tracks(1, license_ccurl="")))
    await _stub_upload(monkeypatch)

    tracks = await fetch_curated_tracks(limit=1)
    assert tracks[0].license_name == "CC BY 3.0"
    assert tracks[0].license_url == lib_module.DEFAULT_LICENSE_URL


def test_license_name_falls_back_to_plain_cc_by_when_the_url_does_not_parse():
    assert lib_module._license_name_from_url("https://example.com/not-a-cc-url") == "CC BY"
    assert lib_module._license_name_from_url("") == "CC BY"


async def test_pagination_skips_a_flaky_empty_first_page_and_still_reaches_the_target(monkeypatch):
    """Real live finding: Jamendo sometimes returns results_count=0 for a
    given offset for no documented reason, then a real page a bit further
    on works fine. Simulates exactly that shape: offset=0 is empty (both
    retry attempts), offset=25 has the real tracks."""
    monkeypatch.setattr(settings, "JAMENDO_CLIENT_ID", "test-client")
    monkeypatch.setattr(lib_module, "_PAGE_SIZE", 25)
    page_25 = _jamendo_tracks(3)
    http = _FakePagedHttp({0: [], 25: page_25})
    monkeypatch.setattr(lib_module.httpx, "AsyncClient", http)
    await _stub_upload(monkeypatch)

    tracks = await fetch_curated_tracks(limit=3)
    assert {t.name for t in tracks} == {"Track 0", "Track 1", "Track 2"}
    assert 0 in http.search_calls
    assert 25 in http.search_calls


async def test_pagination_unions_multiple_pages_to_reach_a_larger_target(monkeypatch):
    monkeypatch.setattr(settings, "JAMENDO_CLIENT_ID", "test-client")
    monkeypatch.setattr(lib_module, "_PAGE_SIZE", 2)
    page_0 = _jamendo_tracks(2)
    page_2 = _jamendo_tracks(2)
    http = _FakePagedHttp({0: page_0, 2: page_2})
    monkeypatch.setattr(lib_module.httpx, "AsyncClient", http)
    await _stub_upload(monkeypatch)

    tracks = await fetch_curated_tracks(limit=4)
    assert len(tracks) == 4
    expected_ids = {t["id"] for t in page_0 + page_2}
    assert {t.source_track_id for t in tracks} == expected_ids


async def test_a_second_run_skips_already_ingested_tracks(monkeypatch):
    monkeypatch.setattr(settings, "JAMENDO_CLIENT_ID", "test-client")
    batch = _jamendo_tracks(2)
    http = _FakeHttp(batch)
    monkeypatch.setattr(lib_module.httpx, "AsyncClient", http)
    await _stub_upload(monkeypatch)

    await fetch_curated_tracks(limit=2)
    first_download_calls = len(http.calls)

    await fetch_curated_tracks(limit=2)
    ids = [t["id"] for t in batch]
    assert await music_library_tracks.count_documents({"source_track_id": {"$in": ids}}) == 2
    # Second run still does the one search call but skips both downloads.
    assert len(http.calls) == first_download_calls + 1


async def test_raises_clearly_when_jamendo_returns_no_tracks(monkeypatch):
    monkeypatch.setattr(settings, "JAMENDO_CLIENT_ID", "test-client")
    monkeypatch.setattr(lib_module.httpx, "AsyncClient", _FakeHttp([]))
    with pytest.raises(MusicLibraryError, match="zero tracks"):
        await fetch_curated_tracks()


# ── merge into the Music tab's bed picker ────────────────────────────────────

async def _seed_one_library_track(monkeypatch) -> str:
    monkeypatch.setattr(settings, "JAMENDO_CLIENT_ID", "test-client")
    monkeypatch.setattr(lib_module.httpx, "AsyncClient", _FakeHttp(_jamendo_tracks(1)))
    await _stub_upload(monkeypatch, url="https://res.cloudinary.com/demo/lib0.mp3")
    tracks = await fetch_curated_tracks(limit=1)
    return tracks[0].id


async def test_kit_out_lists_library_tracks_alongside_the_brand_s_own_beds(signup_user, monkeypatch):
    client, _ = await signup_user()
    ws_id = await create_workspace(client, "Music Lib WS")
    brand_id = await _brand(client, ws_id)
    library_id = await _seed_one_library_track(monkeypatch)

    res = await client.get(f"/api/v1/audio-assets/kit/{brand_id}", headers=_h(ws_id))
    assert res.status_code == 200, res.text
    beds = res.json()["music_beds"]
    lib_bed = next(b for b in beds if b["id"] == library_id)
    assert lib_bed["is_library"] is True
    assert lib_bed["artist"] == "Artist 0"
    assert lib_bed["url"] == "https://res.cloudinary.com/demo/lib0.mp3"


async def test_resolve_music_bed_finds_a_library_track_not_in_the_brand_s_kit(monkeypatch):
    library_id = await _seed_one_library_track(monkeypatch)
    empty_kit = AudioKit(brand_id="b1", workspace_id="w1")

    resolved = await audio_module._resolve_music_bed(empty_kit, library_id)
    assert isinstance(resolved, MusicBed)
    assert resolved.id == library_id

    assert await audio_module._resolve_music_bed(empty_kit, "does-not-exist") is None
    assert await audio_module._resolve_music_bed(empty_kit, None) is None


async def test_list_library_tracks_returns_every_ingested_track(monkeypatch):
    monkeypatch.setattr(settings, "JAMENDO_CLIENT_ID", "test-client")
    batch = _jamendo_tracks(3)
    monkeypatch.setattr(lib_module.httpx, "AsyncClient", _FakeHttp(batch))
    await _stub_upload(monkeypatch)
    ingested = await fetch_curated_tracks(limit=3)

    tracks = await list_library_tracks()
    ingested_ids = {t.id for t in ingested}
    # The library is shared/global, so other tests in this run have also
    # added their own tracks — assert this batch is present, not that the
    # collection is empty otherwise.
    assert ingested_ids.issubset({t.id for t in tracks})
    assert {t.name for t in tracks if t.id in ingested_ids} == {"Track 0", "Track 1", "Track 2"}
