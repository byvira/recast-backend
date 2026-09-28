"""Tests for echo/room-noise reduction (app.pipelines.media.echo_reduction)
and its wiring into the real /cleanup endpoint.

The ElevenLabs key configured for this app is live-confirmed (2026-09-28) to
lack the audio_isolation permission (free plan) — these tests stub the real
HTTP call rather than hitting ElevenLabs, but assert the exact real
behavior observed live: a 401 with status "missing_permissions" surfaces a
specific, member-facing reason, not a generic error.
"""

import pytest

from app.api.v1 import audio_assets as audio_module
from app.core.config import settings
from app.pipelines.media import echo_reduction as echo_module
from app.pipelines.media.echo_reduction import EchoReductionError, reduce_echo
from tests.test_audio_assets import _generate, _h, _setup, _wav, stubs  # noqa: F401 — fixture reuse


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict | None = None, content: bytes = b""):
        self.status_code = status_code
        self._json = json_body or {}
        self.content = content

    def json(self):
        return self._json


class _FakePost:
    """Stands in for httpx.AsyncClient inside echo_reduction — only .post is used."""

    def __init__(self, response: _FakeResponse):
        self.response, self.calls = response, []

    def __call__(self, *a, **kw):
        outer = self

        class _Client:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            async def post(self_inner, url, *, headers=None, files=None, **k):
                outer.calls.append((url, headers, files))
                return outer.response

        return _Client()


# ── reduce_echo() unit tests ─────────────────────────────────────────────────

async def test_refuses_to_run_without_an_api_key(monkeypatch):
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "")
    with pytest.raises(EchoReductionError, match="API key"):
        await reduce_echo(b"audio-bytes")


async def test_returns_the_real_cleaned_bytes_on_success(monkeypatch):
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "el-key")
    fake = _FakePost(_FakeResponse(200, content=b"cleaned-audio"))
    monkeypatch.setattr(echo_module.httpx, "AsyncClient", fake)

    result = await reduce_echo(b"original-audio")
    assert result == b"cleaned-audio"
    url, headers, files = fake.calls[0]
    assert url == echo_module.AUDIO_ISOLATION_URL
    assert headers == {"xi-api-key": "el-key"}
    assert files["audio"][1] == b"original-audio"


async def test_a_missing_permission_401_gives_the_specific_upgrade_reason(monkeypatch):
    """Live-confirmed real ElevenLabs shape: {"detail": {"status": "missing_permissions", ...}}."""
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "el-key")
    body = {"detail": {"type": "authentication_error", "status": "missing_permissions", "message": "..."}}
    fake = _FakePost(_FakeResponse(401, body))
    monkeypatch.setattr(echo_module.httpx, "AsyncClient", fake)

    with pytest.raises(EchoReductionError, match="paid ElevenLabs plan"):
        await reduce_echo(b"audio-bytes")


async def test_a_different_401_gives_a_generic_credentials_reason(monkeypatch):
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "el-key")
    fake = _FakePost(_FakeResponse(401, {"detail": {"status": "invalid_api_key"}}))
    monkeypatch.setattr(echo_module.httpx, "AsyncClient", fake)

    with pytest.raises(EchoReductionError, match="rejected this request's credentials"):
        await reduce_echo(b"audio-bytes")


async def test_a_non_200_non_401_failure_is_reported_clearly(monkeypatch):
    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "el-key")
    fake = _FakePost(_FakeResponse(500, {}))
    monkeypatch.setattr(echo_module.httpx, "AsyncClient", fake)

    with pytest.raises(EchoReductionError, match="Try again"):
        await reduce_echo(b"audio-bytes")


async def test_a_network_failure_is_reported_clearly(monkeypatch):
    import httpx

    monkeypatch.setattr(settings, "ELEVENLABS_API_KEY", "el-key")

    class _Boom:
        def __call__(self, *a, **kw):
            class _Client:
                async def __aenter__(self_inner):
                    return self_inner

                async def __aexit__(self_inner, *exc):
                    return False

                async def post(self_inner, *a, **k):
                    raise httpx.ConnectError("no route")

            return _Client()

    monkeypatch.setattr(echo_module.httpx, "AsyncClient", _Boom())
    with pytest.raises(EchoReductionError, match="Could not reach"):
        await reduce_echo(b"audio-bytes")


# ── wired into the real /cleanup endpoint ────────────────────────────────────

async def test_cleanup_calls_echo_reduction_first_and_records_it(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()

    async def _download(url):
        return _wav(1.0)

    async def _fake_reduce(audio_bytes, filename="audio.wav"):
        assert audio_bytes == _wav(1.0)
        return _wav(1.0)  # stand-in "cleaned" bytes, same shape

    monkeypatch.setattr(audio_module, "_download_media_bytes", _download)
    monkeypatch.setattr(audio_module, "reduce_echo", _fake_reduce)

    res = await client.post(
        f"/api/v1/audio-assets/{asset['id']}/cleanup", json={"remove_echo": True}, headers=_h(ws_id),
    )
    assert res.status_code == 200, res.text
    assert res.json()["dsp_settings"]["cleanup"]["remove_echo"] is True


async def test_cleanup_surfaces_the_real_echo_reduction_error_as_a_400(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()

    async def _download(url):
        return _wav(1.0)

    async def _fake_reduce(audio_bytes, filename="audio.wav"):
        raise EchoReductionError("Echo reduction needs a paid ElevenLabs plan with the audio_isolation permission.")

    monkeypatch.setattr(audio_module, "_download_media_bytes", _download)
    monkeypatch.setattr(audio_module, "reduce_echo", _fake_reduce)

    res = await client.post(
        f"/api/v1/audio-assets/{asset['id']}/cleanup", json={"remove_echo": True}, headers=_h(ws_id),
    )
    assert res.status_code == 400
    assert "paid ElevenLabs plan" in res.json()["detail"]


async def test_remove_echo_alone_is_not_an_empty_cleanup_request(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()

    async def _download(url):
        return _wav(1.0)

    async def _fake_reduce(audio_bytes, filename="audio.wav"):
        return audio_bytes

    monkeypatch.setattr(audio_module, "_download_media_bytes", _download)
    monkeypatch.setattr(audio_module, "reduce_echo", _fake_reduce)

    res = await client.post(
        f"/api/v1/audio-assets/{asset['id']}/cleanup", json={"remove_echo": True}, headers=_h(ws_id),
    )
    assert res.status_code == 200, res.text
