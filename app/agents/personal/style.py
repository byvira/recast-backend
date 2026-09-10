"""Cheap, deterministic text-style features for the voice fingerprint.

No LLM calls here — these run on every observed piece inside the graph, so they
must be fast and dependency-free. They feed the ``style_fingerprint`` on the
persona doc and the human-readable "deltas" the assistant shows a member.
"""

from __future__ import annotations

import re
from collections import Counter

_WORD_RE = re.compile(r"[A-Za-z']+")
_SENT_SPLIT_RE = re.compile(r"[.!?]+(?:\s+|$)")
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)

# Small, generic stopword set — enough to keep keyword sets meaningful without
# pulling in nltk. Provisional; expand if topic-shift proves noisy.
_STOPWORDS = frozenset("""
a an and are as at be been but by can could did do does for from had has have
he her him his how i if in into is it its me my no not of on or our so than that
the their them then there these they this to up us was we were what when which
who will with would you your just like get got make made really very much also
""".split())


def _sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENT_SPLIT_RE.split(text or "") if s.strip()]
    return parts or ([text.strip()] if (text or "").strip() else [])


def fingerprint(text: str) -> dict:
    """Return the per-piece style vector merged into the persona EWMA later."""
    text = text or ""
    words = _WORD_RE.findall(text)
    sents = _sentences(text)
    n_words = len(words) or 1
    n_sents = len(sents) or 1
    lines = [ln for ln in text.splitlines() if ln.strip()]
    list_lines = sum(1 for ln in lines if ln.lstrip()[:2] in ("- ", "* ", "1.", "2.", "3."))

    return {
        "avg_sentence_len": round(n_words / n_sents, 3),
        "avg_word_len": round(sum(len(w) for w in words) / n_words, 3),
        "emoji_rate": round(len(_EMOJI_RE.findall(text)) / n_words, 5),
        "question_rate": round(text.count("?") / n_sents, 5),
        "list_rate": round(list_lines / (len(lines) or 1), 5),
        "reading_grade": _reading_grade(n_words, n_sents, words),
    }


def _reading_grade(n_words: int, n_sents: int, words: list[str]) -> float:
    """Flesch–Kincaid grade, approximated (syllables ~= vowel-group count)."""
    syllables = sum(max(1, len(re.findall(r"[aeiouy]+", w.lower()))) for w in words) or 1
    grade = 0.39 * (n_words / n_sents) + 11.8 * (syllables / n_words) - 15.59
    return round(max(0.0, grade), 2)


def opener(text: str, words: int = 6) -> str:
    """First few words of the piece — used to learn recurring opening patterns."""
    toks = (text or "").strip().split()
    return " ".join(toks[:words]).lower()


def closer(text: str, words: int = 6) -> str:
    toks = (text or "").strip().split()
    return " ".join(toks[-words:]).lower()


def keywords(text: str, top_k: int) -> list[str]:
    """Top-K content keywords (lowercased, stopwords/very-short tokens removed)."""
    counts = Counter(
        w.lower() for w in _WORD_RE.findall(text or "")
        if len(w) > 3 and w.lower() not in _STOPWORDS
    )
    return [w for w, _ in counts.most_common(top_k)]


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _habit_flip(baseline_rate: float, piece_rate: float) -> float:
    """1.0 when a consistent stylistic habit either vanished or newly appeared."""
    if baseline_rate >= 0.04 and piece_rate <= baseline_rate * 0.25:
        return 1.0
    if baseline_rate <= 0.01 and piece_rate >= 0.08:
        return 1.0
    return 0.0


def style_divergence(piece_fp: dict, baseline_fp: dict) -> float:
    """How far this piece's *register* is from the member's baseline.

    0 ≈ same register; ~1 = clearly different; >1.5 = wildly different (e.g.
    casual social voice → formal corporate memo). Deliberately dominated by
    reading grade + sentence length, the features that move most on a real
    voice shift and least on a mere topic change. All weights/scales here are
    provisional and expected to be tuned against real content.
    """
    b_grade = float(baseline_fp.get("reading_grade", 0) or 0)
    p_grade = float(piece_fp.get("reading_grade", 0) or 0)
    b_len = float(baseline_fp.get("avg_sentence_len", 0) or 0)
    p_len = float(piece_fp.get("avg_sentence_len", 0) or 0)

    if b_grade == 0 and b_len == 0:
        return 0.0  # no baseline style yet

    grade_term = min(abs(p_grade - b_grade) / 8.0, 2.0)
    len_ratio = max(p_len, b_len) / max(min(p_len, b_len), 1.0)
    len_term = min((len_ratio - 1.0) / 1.2, 2.0)
    q_term = _habit_flip(
        float(baseline_fp.get("question_rate", 0) or 0), float(piece_fp.get("question_rate", 0) or 0)
    )
    emoji_term = _habit_flip(
        float(baseline_fp.get("emoji_rate", 0) or 0), float(piece_fp.get("emoji_rate", 0) or 0)
    )
    return round(0.5 * grade_term + 0.3 * len_term + 0.1 * q_term + 0.1 * emoji_term, 4)


def style_deltas(piece: dict, baseline: dict) -> list[str]:
    """Human-readable differences between a piece's style and the baseline.

    Only reports differences big enough to be worth mentioning. Thresholds here
    are presentational, not detection logic, so they live inline.
    """
    out: list[str] = []

    def cmp(key: str, label: str, unit: str, rel: float = 0.35, absmin: float = 0.0):
        bv = float(baseline.get(key, 0.0) or 0.0)
        pv = float(piece.get(key, 0.0) or 0.0)
        if bv == 0.0 and pv == 0.0:
            return
        diff = pv - bv
        if abs(diff) < absmin:
            return
        if bv > 0 and abs(diff) / bv < rel:
            return
        direction = "longer" if diff > 0 else "shorter"
        if key not in ("avg_sentence_len", "avg_word_len"):
            direction = "more" if diff > 0 else "less"
        out.append(f"{label}: {pv:g}{unit} vs your usual {bv:g}{unit} ({direction})")

    cmp("avg_sentence_len", "sentence length", " words", absmin=3)
    cmp("emoji_rate", "emoji use", "/word", absmin=0.01)
    cmp("question_rate", "questions", "/sentence", absmin=0.15)
    cmp("list_rate", "bullet/list style", "", absmin=0.2)
    cmp("reading_grade", "reading grade", "", rel=0.25, absmin=2)
    return out
