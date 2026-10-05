"""Checks that generated text is in the language that was asked for.

Two cases are checked, both with plain counting and no model call:
  - a single language with its own script (Tamil, Hindi, Arabic, ...): most letters must be in that script.
  - a mixed code written in Latin letters ("ta+en" Tanglish, "hi+en" Hinglish): the text must carry enough common words
    of the non-English language. A post that is pure English fails.
Anything else (English, languages with no entry here) is not judged, so an unknown language never blocks a post.
"""
from __future__ import annotations

import re
from typing import Optional

_SCRIPT_RANGES: dict[str, list[tuple[int, int]]] = {
    "ta": [(0x0B80, 0x0BFF)],
    "hi": [(0x0900, 0x097F)],
    "mr": [(0x0900, 0x097F)],
    "ne": [(0x0900, 0x097F)],
    "ml": [(0x0D00, 0x0D7F)],
    "te": [(0x0C00, 0x0C7F)],
    "kn": [(0x0C80, 0x0CFF)],
    "bn": [(0x0980, 0x09FF)],
    "gu": [(0x0A80, 0x0AFF)],
    "pa": [(0x0A00, 0x0A7F)],
    "ar": [(0x0600, 0x06FF)],
    "ru": [(0x0400, 0x04FF)],
    "uk": [(0x0400, 0x04FF)],
    "ko": [(0xAC00, 0xD7AF)],
    "ja": [(0x3040, 0x30FF), (0x4E00, 0x9FFF)],
    "zh": [(0x4E00, 0x9FFF)],
}

# Common function words, verbs and connectors people use when typing the language in English letters. Spelling varies,
# so several spellings are listed. Brand and technical terms are not here on purpose.
_ROMANISED_WORDS: dict[str, frozenset[str]] = {
    "ta": frozenset(
        "illa illai irukku irukkum iruku irukkanum irukanum aachu aagum aagum aagudhu panna pannunga pannanum pannu panra "
        "pannuvom pannalam seiyalam seiyunga solla sollunga sollrom solren sollu vendam vendaam venum vaenum vanga vaanga "
        "poga pogalam pochu poitaanga vandhu vandhaanga paaru paarunga paakalam nalla romba konjam rombha thaan dhaan "
        "dhan adhu idhu adhan idhan andha indha ennaku enakku unga ungaluku namma namakku nammaku enna epdi eppadi yen yaen "
        "yaaru yaar enga inga anga appo ippo appadi ippadi mathiri maadhiri pola kitta kooda mela keela udhavum theriyum "
        "theriyala puriyum puriyala vitrunga vittuta aana aanaa illana ellam ellarum oru oruthar naan nee neenga avanga "
        "avar ivanga ivar naanga naama nam ungal ungalukku podhum podhuma mudiyum mudiyadhu mudiyala kedaikum kedaikkum "
        "ponga solrenga seyyunga irundha irundhaal irukkum sari seri nallaa nijam nijama".split()
    ),
    "hi": frozenset(
        "hai hain tha thi hoga hogi nahi nahin kya kaise kyun kyu aap hum tum yeh ye woh wo isko usko iska uska ka ki "
        "ke ko se mein pe aur bhi toh lekin magar agar jab abhi bahut bohot thoda kuch sab koi kar karo karna "
        "karte karke kiya kijiye chahiye chahta chahte milta milte jaana jao jaate raha rahe rahi apna apni apne mera meri "
        "mere tera teri tumhara hamara hamari sirf phir fir waise lagta lagti accha acha sahi galat zyada kam".split()
    ),
}

# Fewer than this share of words (and fewer than the minimum count) means the text is not in the mix that was asked for.
_MIN_SHARE = 0.04
_MIN_HITS = 2
_MIN_WORDS_TO_JUDGE = 15
_MIN_SCRIPT_SHARE = 0.6

_WORD = re.compile(r"[A-Za-z']+")


def _in_ranges(ch: str, ranges: list[tuple[int, int]]) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in ranges)


def _script_share(text: str, ranges: list[tuple[int, int]]) -> Optional[float]:
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 20:
        return None
    return sum(1 for ch in letters if _in_ranges(ch, ranges)) / len(letters)


def language_problem(text: str, language: str) -> Optional[str]:
    """A plain sentence saying what is wrong with the language of `text`, or None when it looks right or cannot be judged."""
    code = (language or "").strip().lower()
    if not code or not text or len(text.split()) < _MIN_WORDS_TO_JUDGE:
        return None

    if "+" in code:
        first, _, second = code.partition("+")
        native = second if first.split("-")[0] == "en" else first
        native = native.split("-")[0]
        # A mix written in English letters must not slip into the native script, even for one word.
        ranges = _SCRIPT_RANGES.get(native)
        if ranges:
            letters = [ch for ch in text if ch.isalpha()]
            # Vowel signs are not letters but are part of a native-script word, so every character in the script counts.
            leaked = sum(1 for ch in text if _in_ranges(ch, ranges))
            if leaked >= 3 or (letters and leaked / len(letters) > 0.02):
                return "The post is not written in English letters only. Write every word of the mix in English letters, with no native script."
        words = _ROMANISED_WORDS.get(native)
        if not words:
            return None
        tokens = [t.lower() for t in _WORD.findall(text)]
        if len(tokens) < _MIN_WORDS_TO_JUDGE:
            return None
        hits = sum(1 for t in tokens if t in words)
        if hits >= _MIN_HITS and hits / len(tokens) >= _MIN_SHARE:
            return None
        return "The post is almost entirely English. It must mix the two languages throughout, as the language instruction says."

    base = code.split("-")[0]
    ranges = _SCRIPT_RANGES.get(base)
    if not ranges:
        return None
    share = _script_share(text, ranges)
    if share is None or share >= _MIN_SCRIPT_SHARE:
        return None
    return "The post is not written in the requested language and script."


def mix_hits(text: str, language: str) -> Optional[int]:
    """How many common words of the non-English half of a mixed code `text` contains, or None for a code that is not mixed
    or has no word list."""
    code = (language or "").strip().lower()
    if "+" not in code:
        return None
    first, _, second = code.partition("+")
    native = (second if first.split("-")[0] == "en" else first).split("-")[0]
    words = _ROMANISED_WORDS.get(native)
    if not words:
        return None
    return sum(1 for t in _WORD.findall(text or "") if t.lower() in words)


def infer_mixed_language(text: str) -> Optional[str]:
    """"ta+en" or "hi+en" when `text` is clearly Latin-letter Tamil or Hindi mixed with English, else None. For callers
    that are not told the language."""
    tokens = [t.lower() for t in _WORD.findall(text or "")]
    if len(tokens) < _MIN_WORDS_TO_JUDGE:
        return None
    for native, words in _ROMANISED_WORDS.items():
        hits = sum(1 for t in tokens if t in words)
        if hits >= 3 and hits / len(tokens) >= 0.08:
            return f"{native}+en"
    return None


def source_language(text: str, fallback: Optional[str] = None) -> str:
    """The language a piece of existing text is written in: detected from its script, or a romanised mix such as Tanglish
    recognised by its words, else `fallback`, else English. For callers that rewrite or judge existing text."""
    from app.shared.language import detect_language

    return detect_language(text) or infer_mixed_language(text) or fallback or "en"
