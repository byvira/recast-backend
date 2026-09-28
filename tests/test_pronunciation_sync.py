"""Tests for the ElevenLabs pronunciation-dictionary sync.

Alias-type rules only: eleven_multilingual_v2 (the model in use) has no
phoneme-tag support at all. Everything here runs against a fake HTTP client —
never a real ElevenLabs call (the live account currently answers 401 for this
endpoint anyway, pending a plan upgrade; the code must fail safe, which is
what the failure tests pin down).
"""

import httpx
import pytest

from app.db.mongo import member_lexicon
from app.models.lexicon import MemberLexicon, PronunciationEntry
from app.models.voice_settings import MemberVoiceSettings
from app.pipelines.media import tts_generation
from tests.conftest import create_workspace


class _FakeResponse:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=httpx.Request("POST", "http://x"), response=self,  # type: ignore[arg-type]
            )

    def json(self):
        return self._payload


def _install_fake_http(monkeypatch, response: _FakeResponse) -> list[dict]:
    """Replaces httpx.AsyncClient inside tts_generation; returns the list the
    fake appends every POST to."""
    calls: list[dict] = []

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None, **kwargs):
            calls.append({"url": url, "headers": headers, "json": json})
            return response

    monkeypatch.setattr(tts_generation.httpx, "AsyncClient", _FakeClient)
    return calls


def _lexicon(entries: list[tuple[str, str]], **extra) -> MemberLexicon:
    return MemberLexicon(
        id="ws:user", workspace_id="ws", user_id="user",
        pronunciations=[
            PronunciationEntry(id=str(i), term=term, ipa=respelling)
            for i, (term, respelling) in enumerate(entries)
        ],
        **extra,
    )


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(tts_generation.settings, "ELEVENLABS_API_KEY", "test-key")


# ── sync_pronunciation_dictionary ────────────────────────────────────────────

async def test_first_sync_creates_a_dictionary_with_alias_rules(monkeypatch, with_key):
    calls = _install_fake_http(monkeypatch, _FakeResponse(200, {"id": "dict-1", "version_id": "ver-1"}))

    result = await tts_generation.sync_pronunciation_dictionary(
        workspace_id="ws", user_id="user", lexicon=_lexicon([("Zendly", "zen-dlee")]),
    )

    assert result == {"id": "dict-1", "version_id": "ver-1"}
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/v1/pronunciation-dictionaries/add-from-rules")
    assert calls[0]["headers"]["xi-api-key"] == "test-key"
    assert calls[0]["json"]["rules"] == [
        {"string_to_replace": "Zendly", "type": "alias", "alias": "zen-dlee"},
    ]
    assert len(calls[0]["json"]["name"]) <= 50


async def test_later_syncs_replace_rules_on_the_existing_dictionary(monkeypatch, with_key):
    calls = _install_fake_http(monkeypatch, _FakeResponse(200, {"id": "dict-1", "version_id": "ver-2"}))

    result = await tts_generation.sync_pronunciation_dictionary(
        workspace_id="ws", user_id="user",
        lexicon=_lexicon([("Zendly", "zen-dlee")], elevenlabs_dictionary_id="dict-1"),
    )

    assert result == {"id": "dict-1", "version_id": "ver-2"}
    assert calls[0]["url"].endswith("/v1/pronunciation-dictionaries/dict-1/add-rules")


async def test_blank_entries_are_skipped(monkeypatch, with_key):
    calls = _install_fake_http(monkeypatch, _FakeResponse(200, {"id": "d", "version_id": "v"}))

    await tts_generation.sync_pronunciation_dictionary(
        workspace_id="ws", user_id="user",
        lexicon=_lexicon([("Zendly", "zen-dlee"), ("  ", "x"), ("Blank", "  ")]),
    )
    assert [r["string_to_replace"] for r in calls[0]["json"]["rules"]] == ["Zendly"]


async def test_nothing_to_sync_makes_no_call(monkeypatch, with_key):
    calls = _install_fake_http(monkeypatch, _FakeResponse(200, {"id": "d", "version_id": "v"}))

    assert await tts_generation.sync_pronunciation_dictionary(
        workspace_id="ws", user_id="user", lexicon=_lexicon([]),
    ) is None
    assert await tts_generation.sync_pronunciation_dictionary(
        workspace_id="ws", user_id="user", lexicon=_lexicon([("  ", "  ")]),
    ) is None
    assert calls == []


async def test_no_api_key_makes_no_call(monkeypatch):
    monkeypatch.setattr(tts_generation.settings, "ELEVENLABS_API_KEY", "")
    calls = _install_fake_http(monkeypatch, _FakeResponse(200, {"id": "d", "version_id": "v"}))

    result = await tts_generation.sync_pronunciation_dictionary(
        workspace_id="ws", user_id="user", lexicon=_lexicon([("Zendly", "zen-dlee")]),
    )
    assert result is None
    assert calls == []


async def test_a_401_from_elevenlabs_fails_safe_and_never_raises(monkeypatch, with_key):
    """The live account's real answer today (plan doesn't include the endpoint)."""
    _install_fake_http(monkeypatch, _FakeResponse(401))

    result = await tts_generation.sync_pronunciation_dictionary(
        workspace_id="ws", user_id="user", lexicon=_lexicon([("Zendly", "zen-dlee")]),
    )
    assert result is None


# ── synthesize_speech attaches the locators ──────────────────────────────────

def _voice() -> MemberVoiceSettings:
    return MemberVoiceSettings(id="ws:user", workspace_id="ws", user_id="user")


async def test_synthesis_attaches_locators_when_the_lexicon_is_synced(monkeypatch, with_key):
    seen: dict = {}

    async def _fake_elevenlabs(**kwargs):
        seen.update(kwargs)
        return b"audio"

    monkeypatch.setattr(tts_generation, "_call_elevenlabs", _fake_elevenlabs)

    audio = await tts_generation.synthesize_speech(
        text="Hi", voice_settings=_voice(), workspace_id="ws", user_id="user",
        lexicon=_lexicon(
            [("Zendly", "zen-dlee")],
            elevenlabs_dictionary_id="dict-1", elevenlabs_dictionary_version_id="ver-9",
        ),
    )

    assert audio == b"audio"
    assert seen["pronunciation_dictionary_locators"] == [
        {"pronunciation_dictionary_id": "dict-1", "version_id": "ver-9"},
    ]


async def test_synthesis_sends_no_locators_when_unsynced_or_no_lexicon(monkeypatch, with_key):
    seen: list = []

    async def _fake_elevenlabs(**kwargs):
        seen.append(kwargs["pronunciation_dictionary_locators"])
        return b"audio"

    monkeypatch.setattr(tts_generation, "_call_elevenlabs", _fake_elevenlabs)

    await tts_generation.synthesize_speech(
        text="Hi", voice_settings=_voice(), workspace_id="ws", user_id="user",
        lexicon=_lexicon([("Zendly", "zen-dlee")]),  # entries but never synced
    )
    await tts_generation.synthesize_speech(
        text="Hi", voice_settings=_voice(), workspace_id="ws", user_id="user", lexicon=None,
    )
    assert seen == [None, None]


async def test_deepgram_fallback_still_narrates_and_ignores_the_lexicon(monkeypatch, with_key):
    """Deepgram's Aura API has no pronunciation mechanism — narration must
    still succeed, just without it."""
    async def _fail(**kwargs):
        raise RuntimeError("elevenlabs unavailable")

    async def _deepgram(**kwargs):
        return b"deepgram-audio"

    monkeypatch.setattr(tts_generation, "_call_elevenlabs", _fail)
    monkeypatch.setattr(tts_generation, "_call_deepgram", _deepgram)
    monkeypatch.setattr(tts_generation.settings, "DEEPGRAM_API_KEY", "dg-key")

    audio = await tts_generation.synthesize_speech(
        text="Hi", voice_settings=_voice(), workspace_id="ws", user_id="user",
        lexicon=_lexicon([("Zendly", "zen-dlee")], elevenlabs_dictionary_id="d", elevenlabs_dictionary_version_id="v"),
    )
    assert audio == b"deepgram-audio"


# ── PUT /assistant/lexicon wires it together ─────────────────────────────────

_LEXICON_BODY = {
    "pronunciations": [{"id": "", "term": "Zendly", "ipa": "zen-dlee", "notes": ""}],
    "whitelist": ["SaaS"],
    "blacklist": ["leverage"],
    "writing_blueprint": {},
}


async def test_saving_the_lexicon_persists_the_synced_dictionary_ids(signup_user, monkeypatch):
    from app.api.v1 import assistant as assistant_module

    async def _fake_sync(*, workspace_id, user_id, lexicon):
        assert [p.term for p in lexicon.pronunciations] == ["Zendly"]
        return {"id": "dict-7", "version_id": "ver-3"}

    monkeypatch.setattr(assistant_module, "sync_pronunciation_dictionary", _fake_sync)

    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Lexicon Sync")

    res = await client.put("/api/v1/assistant/lexicon", json=_LEXICON_BODY, headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    assert res.json()["elevenlabs_dictionary_id"] == "dict-7"
    assert res.json()["elevenlabs_dictionary_version_id"] == "ver-3"

    stored = await member_lexicon.find_one({"workspace_id": ws_id, "user_id": profile["id"]})
    assert stored["elevenlabs_dictionary_id"] == "dict-7"
    assert stored["blacklist"] == ["leverage"]


async def test_a_failed_sync_never_blocks_the_lexicon_save(signup_user, monkeypatch):
    from app.api.v1 import assistant as assistant_module

    async def _failing_sync(**kwargs):
        return None

    monkeypatch.setattr(assistant_module, "sync_pronunciation_dictionary", _failing_sync)

    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Lexicon Sync Fail")

    res = await client.put("/api/v1/assistant/lexicon", json=_LEXICON_BODY, headers={"X-Workspace-Id": ws_id})
    assert res.status_code == 200, res.text
    assert res.json()["elevenlabs_dictionary_id"] is None
    assert [p["term"] for p in res.json()["pronunciations"]] == ["Zendly"]

    stored = await member_lexicon.find_one({"workspace_id": ws_id, "user_id": profile["id"]})
    assert stored["pronunciations"][0]["term"] == "Zendly"
    assert stored.get("elevenlabs_dictionary_id") is None
