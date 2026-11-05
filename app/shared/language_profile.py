"""What language a text is in, and whether plain English rules are enough to check it.

One chain, used by the safety and quality checks and open to every other part of the product:

  1. the language the post was asked for (a mixed one such as "ta+en" for Tanglish), because the product already knows it;
  2. the script the text is written in (Devanagari, Tamil, Arabic and so on), counted by letters;
  3. the language a text detector reads from the words, when it is sure;
  4. unknown.

Every step is open-ended: nothing here checks a language against a fixed list, so a language the product has never heard of still
goes through the chain. The result says whether the English-wording rules can read the text (`rules_can_read`); when they cannot, the
meaning-level read by a model is the check that matters, so callers send such text to it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# (first code point, last code point, script name): the scripts the product's languages are written in.
_SCRIPT_RANGES: tuple[tuple[int, int, str], ...] = (
    (0x0370, 0x03FF, "greek"), (0x0400, 0x04FF, "cyrillic"), (0x0590, 0x05FF, "hebrew"), (0x0600, 0x06FF, "arabic"),
    (0x0750, 0x077F, "arabic"), (0x0900, 0x097F, "devanagari"), (0x0980, 0x09FF, "bengali"), (0x0A00, 0x0A7F, "gurmukhi"),
    (0x0A80, 0x0AFF, "gujarati"), (0x0B00, 0x0B7F, "odia"), (0x0B80, 0x0BFF, "tamil"), (0x0C00, 0x0C7F, "telugu"),
    (0x0C80, 0x0CFF, "kannada"), (0x0D00, 0x0D7F, "malayalam"), (0x0D80, 0x0DFF, "sinhala"), (0x0E00, 0x0E7F, "thai"),
    (0x1100, 0x11FF, "hangul"), (0x3040, 0x30FF, "kana"), (0x4E00, 0x9FFF, "han"), (0xAC00, 0xD7AF, "hangul"),
)
# The language usually written in each script, used only when nothing asked for a language and the detector is unsure.
_SCRIPT_LANGUAGE = {
    "devanagari": "hi", "bengali": "bn", "gurmukhi": "pa", "gujarati": "gu", "odia": "or", "tamil": "ta", "telugu": "te",
    "kannada": "kn", "malayalam": "ml", "sinhala": "si", "thai": "th", "arabic": "ar", "hebrew": "he", "cyrillic": "ru",
    "greek": "el", "hangul": "ko", "kana": "ja", "han": "zh",
}
MIN_SCRIPT_SHARE = 0.3


@dataclass(frozen=True)
class LanguageProfile:
    code: str  # the best known language code, or "unknown"
    source: str  # "requested", "script", "detected" or "unknown"
    script: str  # "latin" or the dominant script
    mixed: bool  # a deliberate blend such as Tanglish
    rules_can_read: bool  # whether rules written in English wording are enough to check this text
    confidence: str  # "stated" (asked for), "measured" (read from the text) or "none"

    @property
    def regional(self) -> bool:
        """Text that needs a meaning-level read because the English-wording rules cannot judge it."""
        return not self.rules_can_read


def script_of(text: str) -> tuple[str, float]:
    """The dominant script among the letters of the text and its share of them. Latin when no other script reaches the minimum share."""
    counts: dict[str, int] = {}
    letters = 0
    for ch in text or "":
        if not ch.isalpha():
            continue
        letters += 1
        cp = ord(ch)
        for lo, hi, name in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                counts[name] = counts.get(name, 0) + 1
                break
    if not letters or not counts:
        return "latin", 1.0
    name, n = max(counts.items(), key=lambda kv: kv[1])
    share = n / letters
    return (name, share) if share >= MIN_SCRIPT_SHARE else ("latin", 1 - share)


def _base(code: str) -> str:
    return (code or "").strip().lower().split("+")[0].split("-")[0].split("_")[0]


def profile(text: str, requested: Optional[str] = None) -> LanguageProfile:
    """The language profile of `text`, following the chain in the module notes. Never raises: a text nothing can place is `unknown`."""
    script, _ = script_of(text)
    asked = (requested or "").strip().lower()
    mixed = "+" in asked
    if asked:
        base = _base(asked)
        english_only = base == "en" and not mixed and script == "latin"
        return LanguageProfile(asked, "requested", script, mixed, english_only, "stated")
    if script != "latin":
        return LanguageProfile(_SCRIPT_LANGUAGE.get(script, "unknown"), "script", script, False, False, "measured")
    detected = None
    try:
        from app.shared.language import detect_language

        detected = detect_language(text)
    except Exception:  # noqa: BLE001 - a detector problem must never stop a check
        detected = None
    if detected:
        return LanguageProfile(detected, "detected", "latin", False, _base(detected) == "en", "measured")
    # Nothing is sure. Plain English is the common case and the rules read it; anything else would have shown a script or a detection.
    return LanguageProfile("unknown", "unknown", "latin", False, True, "none")
