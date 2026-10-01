"""Real TTS integration — ElevenLabs (the deliberate product decision, see
pow/audio_image_pipeline/02-audio-pipeline-plan.md's TTS integration
section) with a real Deepgram Aura fallback added 2026-09-26 — real,
live-tested 2026-09-26 (not assumed): a genuine 200 with real audio bytes
came back on the first try, no card required for its $200 free credit
(unlike Azure, which was tried first, live-verified, then explicitly
removed the same day per the user's choice — Azure's own account creation
needs a real card despite its F0 pricing tier being $0; Deepgram has no
such requirement, which is why it stuck where Azure didn't).

Deliberately its own accounts, not Cloudflare Workers AI's TTS models
(`@cf/deepgram/aura-*`) — those share the same 10,000-neuron/day pool as
Flux image generation, and one narration could burn most of that shared
budget in a single call. Same budget-isolation reasoning that made
Cloudflare the pick for images in the first place. (Deepgram's own
standalone Aura API, used here, is a separate account/product from
Cloudflare's Workers-AI-hosted Aura models — not the same budget.)

Follows the same defensive shape as image_generation.py's
_generate_image_bytes: never raises, returns None on any failure.
"""

from app.shared.llm_health.track import fallback_scope, note_fallback_failed, refused_outright, track
import time
import base64
import logging
from typing import Optional

import httpx

from pydantic import BaseModel

from app.core.config import settings
from app.models.audio_asset import TranscriptWord
from app.models.lexicon import MemberLexicon
from app.models.voice_settings import MemberVoiceSettings

logger = logging.getLogger(__name__)

_ELEVENLABS_MODEL_FOR_LOG = "text-to-speech"  # the health log groups ElevenLabs speech under one name
_ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
_ELEVENLABS_MODEL_ID = "eleven_multilingual_v2"

_ELEVENLABS_DICT_CREATE_URL = "https://api.elevenlabs.io/v1/pronunciation-dictionaries/add-from-rules"
_ELEVENLABS_DICT_ADD_RULES_URL = "https://api.elevenlabs.io/v1/pronunciation-dictionaries/{dictionary_id}/add-rules"

_DEEPGRAM_TTS_URL = "https://api.deepgram.com/v1/speak"
# A real Deepgram Aura-2 voice, confirmed live 2026-09-26 (a real 200 with
# real audio bytes on the first try) — used as the safe default the same
# way Azure's en-US-JennyNeural was: Deepgram's free credit covers any
# public model, not gated by a paid-plan restriction the way ElevenLabs'
# library voices are, so defaulting to a specific real voice here is safe.
_DEEPGRAM_DEFAULT_VOICE = "aura-2-thalia-en"

# MemberVoiceSettings.tts_voice's own scaffolding default — a Piper (an
# unrelated open-source TTS engine) voice name, not a real ElevenLabs
# voice_id. Calling ElevenLabs with this as-is would fail. See
# pow/audio_image_pipeline/GAPS.md G-8: no hardcoded real fallback
# voice_id is substituted for ElevenLabs specifically — its own free
# legacy "Default voices" (Rachel/Adam/etc.) only exist for accounts
# created before March 2026 and expire entirely by Dec 31 2026, and this
# is a brand-new account, so guessing one of those IDs could be simply
# wrong on this specific account. Fails clearly instead of guessing.
_PLACEHOLDER_VOICE_IDS = {"piper_lessac", ""}

# Real, confirmed-live language coverage per provider (2026-09-26, WebFetch
# against each provider's own current docs, not assumed) — used by
# audio_assets.py's /localize endpoint to fail clearly *before* spending a
# translation call, rather than after, when the target language isn't
# actually synthesizable by whichever provider will really serve the
# request. ElevenLabs' eleven_multilingual_v2 real list (29 languages,
# confirmed via elevenlabs.io/docs/models):
ELEVENLABS_SUPPORTED_LANGUAGES = {
    "english", "japanese", "chinese", "german", "hindi", "french", "korean",
    "portuguese", "italian", "spanish", "indonesian", "dutch", "turkish",
    "filipino", "polish", "swedish", "bulgarian", "romanian", "arabic",
    "czech", "greek", "finnish", "croatian", "malay", "slovak", "danish",
    "tamil", "ukrainian", "russian",
}
# Deepgram Aura/Aura-2's real list (7 languages, confirmed via
# developers.deepgram.com/docs/tts-models) — notably NOT including Tamil,
# Hindi, or most other languages ElevenLabs covers. This matters right now
# because ElevenLabs is still on the free plan (see PROGRESS.md's
# Blockers) — Deepgram is the only provider actually reachable today.
DEEPGRAM_SUPPORTED_LANGUAGES = {"english", "spanish", "german", "french", "dutch", "italian", "japanese"}


def is_language_supported(language: str) -> tuple[bool, str]:
    """Real check against whichever provider(s) are actually configured,
    not just ElevenLabs' broader list — a language ElevenLabs supports
    but Deepgram doesn't is NOT usable right now if only DEEPGRAM_API_KEY
    is set (today's real state). Returns (supported, reason-if-not)."""
    lang = (language or "").strip().lower()
    if settings.ELEVENLABS_API_KEY and lang in ELEVENLABS_SUPPORTED_LANGUAGES:
        return True, ""
    if settings.DEEPGRAM_API_KEY and lang in DEEPGRAM_SUPPORTED_LANGUAGES:
        return True, ""
    if not settings.ELEVENLABS_API_KEY and not settings.DEEPGRAM_API_KEY:
        return False, "No TTS provider is configured at all."
    # Real, specific reason — which provider(s) are actually reachable
    # today and what they each really support, not a generic "unsupported".
    reachable = []
    if settings.DEEPGRAM_API_KEY:
        reachable.append(f"Deepgram (supports: {', '.join(sorted(DEEPGRAM_SUPPORTED_LANGUAGES))})")
    if settings.ELEVENLABS_API_KEY:
        reachable.append(f"ElevenLabs (supports: {', '.join(sorted(ELEVENLABS_SUPPORTED_LANGUAGES))})")
    return False, f"'{language}' isn't supported by any reachable provider right now — {'; '.join(reachable)}."


def _resolve_elevenlabs_voice_id(voice_settings: MemberVoiceSettings) -> Optional[str]:
    voice_id = (voice_settings.tts_voice or "").strip()
    if voice_id.lower() in _PLACEHOLDER_VOICE_IDS:
        return None
    return voice_id


def _resolve_deepgram_model(voice_settings: MemberVoiceSettings) -> str:
    """Unlike ElevenLabs, a real universal default is safe here (see
    _DEEPGRAM_DEFAULT_VOICE's own comment) — only used if tts_voice
    doesn't already look like a real Aura-2 model name (the
    `aura-2-{name}-{lang}` shape)."""
    voice_id = (voice_settings.tts_voice or "").strip()
    if voice_id.lower() in _PLACEHOLDER_VOICE_IDS:
        return _DEEPGRAM_DEFAULT_VOICE
    if voice_id.startswith("aura-"):
        return voice_id
    return _DEEPGRAM_DEFAULT_VOICE


async def sync_pronunciation_dictionary(
    *, workspace_id: str, user_id: str, lexicon: MemberLexicon,
) -> Optional[dict]:
    """Real ElevenLabs pronunciation-dictionary sync (confirmed live
    2026-09-27, not assumed) — creates the dictionary on first save,
    replaces its rules on every save after (add-rules replaces a rule with
    the same string_to_replace, so this is idempotent, not additive-forever).

    Uses "alias" rules, not "phoneme": eleven_multilingual_v2 (the model
    this codebase synthesizes with) has zero phoneme-tag support at all —
    confirmed against ElevenLabs' own docs — only eleven_flash_v2
    (English-only) or eleven_v3 support phoneme rules. Alias rules are a
    plain text substitution, so they work with any model, but that also
    means PronunciationEntry.ipa must be a real phonetic respelling
    ("zen-dlee"), not IPA notation — see that field's own docstring.

    Never raises — returns None on any failure (no API key, no real
    pronunciation entries, or the HTTP call itself failing), same
    defensive contract as every other provider call in this module. The
    caller persists the returned id/version_id; a None return just means
    the next synthesis call has no locators to attach, not a hard error.
    """
    if not settings.ELEVENLABS_API_KEY:
        return None

    rules = [
        {
            "string_to_replace": p.term,
            "type": "alias",
            "alias": p.ipa,
        }
        for p in lexicon.pronunciations
        if p.term.strip() and p.ipa.strip()
    ]
    if not rules:
        return None

    headers = {"xi-api-key": settings.ELEVENLABS_API_KEY, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if lexicon.elevenlabs_dictionary_id:
                response = await client.post(
                    _ELEVENLABS_DICT_ADD_RULES_URL.format(dictionary_id=lexicon.elevenlabs_dictionary_id),
                    headers=headers,
                    json={"rules": rules},
                )
            else:
                response = await client.post(
                    _ELEVENLABS_DICT_CREATE_URL,
                    headers=headers,
                    json={"name": f"recast-member-{workspace_id}-{user_id}"[:50], "rules": rules},
                )
            response.raise_for_status()
            data = response.json()
            return {"id": data["id"], "version_id": data["version_id"]}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ElevenLabs pronunciation-dictionary sync failed for workspace %s, user %s: %s",
            workspace_id, user_id, exc,
        )
        return None


def _build_elevenlabs_request(
    *,
    text: str,
    voice_settings: MemberVoiceSettings,
    workspace_id: str,
    pronunciation_dictionary_locators: Optional[list[dict]] = None,
    with_timestamps: bool = False,
) -> tuple[str, dict, dict]:
    """The request shared by the plain and the timed call: same voice, same
    settings, same pronunciation locators, so the two produce the same speech."""
    voice_id = _resolve_elevenlabs_voice_id(voice_settings)
    if not voice_id:
        raise RuntimeError(
            f"No real ElevenLabs voice configured for workspace {workspace_id} "
            f"(tts_voice={voice_settings.tts_voice!r} is a placeholder)."
        )

    body = {
        "text": text,
        "model_id": _ELEVENLABS_MODEL_ID,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": min(1.0, max(0.0, voice_settings.vocal_energy / 100)),
            "speed": voice_settings.speech_speed,
        },
    }
    if pronunciation_dictionary_locators:
        body["pronunciation_dictionary_locators"] = pronunciation_dictionary_locators
    headers = {"xi-api-key": settings.ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    url = _ELEVENLABS_TTS_URL.format(voice_id=voice_id)
    if with_timestamps:
        url += "/with-timestamps"
    return url, headers, body


async def _call_elevenlabs(
    *,
    text: str,
    voice_settings: MemberVoiceSettings,
    workspace_id: str,
    pronunciation_dictionary_locators: Optional[list[dict]] = None,
) -> bytes:
    async with track("elevenlabs", _ELEVENLABS_MODEL_FOR_LOG, feature="text_to_speech"):
        return await _call_elevenlabs_raw(text=text, voice_settings=voice_settings, workspace_id=workspace_id, pronunciation_dictionary_locators=pronunciation_dictionary_locators)


async def _call_elevenlabs_raw(
    *,
    text: str,
    voice_settings: MemberVoiceSettings,
    workspace_id: str,
    pronunciation_dictionary_locators: Optional[list[dict]] = None,
) -> bytes:
    """Real ElevenLabs call. Maps MemberVoiceSettings' already-real,
    already-persisted fields onto ElevenLabs' actual request shape
    (confirmed live against ElevenLabs' API docs 2026-09-26):
      - speech_speed (0.8-1.5, already validated on write) -> voice_settings.speed
      - pitch_shift_semitones / emotional_tone -> no direct ElevenLabs
        equivalent exists (their voice_settings has no pitch knob) — a
        real, honest mapping gap, not silently faked. vocal_energy
        loosely informs `style` (0.0-1.0) as the closest available knob.
      - pause_cadence has no ElevenLabs equivalent at all — not mapped.
    Raises on any failure — the caller (synthesize_speech) decides
    whether to fall through to Deepgram, same contract as
    image_generation.py's _call_cloudflare.
    """
    url, headers, body = _build_elevenlabs_request(
        text=text, voice_settings=voice_settings, workspace_id=workspace_id,
        pronunciation_dictionary_locators=pronunciation_dictionary_locators,
    )
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(url, headers=headers, json=body)
        response.raise_for_status()
        return response.content


def alignment_to_words(alignment: dict) -> list[TranscriptWord]:
    """Turns ElevenLabs' per-character timing into per-word timing.

    A word runs from the first letter's start to the last letter's end, and
    keeps the punctuation attached to it (good for captions). Anything that
    doesn't add up (missing arrays, mismatched lengths) yields no words at
    all rather than guessed ones."""
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    if not chars or not (len(chars) == len(starts) == len(ends)):
        return []

    words: list[TranscriptWord] = []
    current: list[str] = []
    w_start = w_end = 0.0
    for ch, st, en in zip(chars, starts, ends):
        if ch.isspace():
            if current:
                words.append(TranscriptWord(word="".join(current), start_s=round(w_start, 3), end_s=round(w_end, 3)))
                current = []
            continue
        if not current:
            w_start = float(st)
        current.append(ch)
        w_end = float(en)
    if current:
        words.append(TranscriptWord(word="".join(current), start_s=round(w_start, 3), end_s=round(w_end, 3)))
    return words


async def _call_elevenlabs_timed(
    *,
    text: str,
    voice_settings: MemberVoiceSettings,
    workspace_id: str,
    pronunciation_dictionary_locators: Optional[list[dict]] = None,
) -> tuple[bytes, list[TranscriptWord]]:
    async with track("elevenlabs", _ELEVENLABS_MODEL_FOR_LOG, feature="text_to_speech"):
        return await _call_elevenlabs_timed_raw(text=text, voice_settings=voice_settings, workspace_id=workspace_id, pronunciation_dictionary_locators=pronunciation_dictionary_locators)


async def _call_elevenlabs_timed_raw(
    *,
    text: str,
    voice_settings: MemberVoiceSettings,
    workspace_id: str,
    pronunciation_dictionary_locators: Optional[list[dict]] = None,
) -> tuple[bytes, list[TranscriptWord]]:
    """The same speech as _call_elevenlabs, plus the exact time each word is
    spoken, from ElevenLabs' with-timestamps endpoint (same price, same
    voice). Raises on any failure, like _call_elevenlabs."""
    url, headers, body = _build_elevenlabs_request(
        text=text, voice_settings=voice_settings, workspace_id=workspace_id,
        pronunciation_dictionary_locators=pronunciation_dictionary_locators, with_timestamps=True,
    )
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(url, headers=headers, json=body)
        response.raise_for_status()
        data = response.json()
    audio = base64.b64decode(data["audio_base64"])
    return audio, alignment_to_words(data.get("alignment") or {})


async def _call_deepgram(*, text: str, voice_settings: MemberVoiceSettings) -> bytes:
    async with track("deepgram", _resolve_deepgram_model(voice_settings), feature="text_to_speech"):
        return await _call_deepgram_raw(text=text, voice_settings=voice_settings)


async def _call_deepgram_raw(*, text: str, voice_settings: MemberVoiceSettings) -> bytes:
    """Real Deepgram Aura-2 call — REST API confirmed live 2026-09-26
    (own docs fetch + a real synthesis test, not assumed): `Authorization:
    Token <key>` auth, plain JSON body ({"text": ...}), model/encoding as
    query params. Aura's API exposes no speed/pitch/style controls at
    all (confirmed against its own docs) — voice_settings.speech_speed/
    pitch_shift_semitones/vocal_energy have no real mapping here, a
    genuine, honest gap (not something ElevenLabs or Azure also lacked;
    this one's specific to Aura's simpler API surface), not silently
    faked with a made-up parameter.
    """
    model = _resolve_deepgram_model(voice_settings)
    headers = {"Authorization": f"Token {settings.DEEPGRAM_API_KEY}", "Content-Type": "application/json"}
    params = {"model": model, "encoding": "mp3"}

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(_DEEPGRAM_TTS_URL, headers=headers, params=params, json={"text": text})
        response.raise_for_status()
        return response.content


class SpeechResult(BaseModel):
    """Synthesized audio, plus per-word timing when the provider reported it
    (ElevenLabs does; the Deepgram fallback does not, so `words` is None)."""
    audio: bytes
    words: Optional[list[TranscriptWord]] = None


def respell(text: str, lexicon: Optional[MemberLexicon]) -> str:
    """Replaces each pronunciation term with its respelling (for example "Zendly" with "zen-dlee"), whole words only and
    ignoring case, so a provider that cannot take a pronunciation dictionary still says the word the member's way."""
    if not lexicon or not lexicon.pronunciations:
        return text
    import re

    for entry in sorted(lexicon.pronunciations, key=lambda e: len(e.term), reverse=True):
        term, spoken = (entry.term or "").strip(), (entry.ipa or "").strip()
        if term and spoken:
            text = re.sub(rf"(?<!\w){re.escape(term)}(?!\w)", lambda _m, s=spoken: s, text, flags=re.IGNORECASE)
    return text


_elevenlabs_skip_until = 0.0  # time.monotonic() until which ElevenLabs is not tried after a plan or key refusal


async def _synthesize(
    *,
    text: str,
    voice_settings: MemberVoiceSettings,
    lexicon: Optional[MemberLexicon],
    workspace_id: str,
    user_id: str,
    timed: bool,
) -> Optional[SpeechResult]:
    """Tries ElevenLabs first (the real product decision); on any
    failure — no key, no real voice configured, the free-plan library-
    voice restriction, or the HTTP call itself failing — falls through to
    Deepgram Aura if configured. Returns None only if neither provider is
    usable. Never raises.

    `lexicon`: if it has a synced elevenlabs_dictionary_id (see
    sync_pronunciation_dictionary, called from PUT /assistant/lexicon on
    save), those locators are attached to the ElevenLabs request for real
    pronunciation control. Deepgram's API still has no pronunciation-
    override mechanism of any kind, so the fallback path never applies
    the lexicon regardless — a genuine per-provider gap, not a bug here.
    """
    global _elevenlabs_skip_until
    pronunciation_locators = None
    if lexicon and lexicon.elevenlabs_dictionary_id and lexicon.elevenlabs_dictionary_version_id:
        pronunciation_locators = [
            {
                "pronunciation_dictionary_id": lexicon.elevenlabs_dictionary_id,
                "version_id": lexicon.elevenlabs_dictionary_version_id,
            }
        ]

    if settings.ELEVENLABS_API_KEY and settings.ELEVENLABS_ENABLED and time.monotonic() >= _elevenlabs_skip_until:
        try:
            if timed:
                audio, words = await _call_elevenlabs_timed(
                    text=text, voice_settings=voice_settings, workspace_id=workspace_id,
                    pronunciation_dictionary_locators=pronunciation_locators,
                )
                return SpeechResult(audio=audio, words=words or None)
            return SpeechResult(audio=await _call_elevenlabs(
                text=text, voice_settings=voice_settings, workspace_id=workspace_id,
                pronunciation_dictionary_locators=pronunciation_locators,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ElevenLabs TTS failed for workspace %s, checking Deepgram fallback: %s",
                workspace_id, exc,
            )
            if refused_outright(exc):
                _elevenlabs_skip_until = time.monotonic() + 600.0  # a plan or key problem: stop asking for 10 minutes
    elif not settings.ELEVENLABS_ENABLED:
        logger.info("ElevenLabs is switched off (ELEVENLABS_ENABLED=false), using Deepgram.")
    else:
        logger.warning("ELEVENLABS_API_KEY not configured for workspace %s.", workspace_id)

    if not settings.DEEPGRAM_API_KEY:
        logger.warning("DEEPGRAM_API_KEY not configured — no TTS provider available.")
        return None

    try:
        with fallback_scope():
            # Deepgram has no pronunciation dictionary, so the member's pronunciations are applied as plain respelling
            audio = await _call_deepgram(text=respell(text, lexicon), voice_settings=voice_settings)
        logger.info("ElevenLabs unavailable — served this narration via the Deepgram fallback.")
        return SpeechResult(audio=audio)
    except Exception as exc:  # noqa: BLE001
        logger.error("Deepgram TTS fallback also failed for workspace %s: %s", workspace_id, exc)
        if settings.ELEVENLABS_API_KEY:
            note_fallback_failed("deepgram", "speech", "text_to_speech", "ElevenLabs failed and the Deepgram fallback failed too.")
        return None


async def synthesize_speech(
    *,
    text: str,
    voice_settings: MemberVoiceSettings,
    lexicon: Optional[MemberLexicon] = None,
    workspace_id: str,
    user_id: str,
) -> Optional[bytes]:
    """Audio only. See _synthesize for the provider order and the lexicon."""
    result = await _synthesize(
        text=text, voice_settings=voice_settings, lexicon=lexicon,
        workspace_id=workspace_id, user_id=user_id, timed=False,
    )
    return result.audio if result else None


async def synthesize_speech_timed(
    *,
    text: str,
    voice_settings: MemberVoiceSettings,
    lexicon: Optional[MemberLexicon] = None,
    workspace_id: str,
    user_id: str,
) -> Optional[SpeechResult]:
    """Audio plus the exact time of every word when the provider gives it, so
    a recording made here has a real transcript from the start."""
    return await _synthesize(
        text=text, voice_settings=voice_settings, lexicon=lexicon,
        workspace_id=workspace_id, user_id=user_id, timed=True,
    )
