"""Claims and personal details, found with plain rules (no model call).

Two kinds of text the safety screen did not look for before:
- claims a brand cannot back up (guaranteed returns, cures, "risk free"), and
- personal or financial details (Aadhaar, PAN, card and bank codes, and phone numbers or emails that are not the brand's own).

The patterns work on the structure of the text, not on one language: every script's digits are read as digits, so an Aadhaar
number written in Devanagari or Tamil numerals is found the same as one in plain digits. Words in other languages are left to the
model read, which judges meaning in any language; the rules here only cover the English wording and the number formats.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

CLAIMS = "claims"
PERSONAL = "personal_data"

Hit = tuple[str, str, tuple[int, int]]  # (category, what to show, where in the text)


def ascii_digits(text: str) -> str:
    """The text with every script's digits written as 0 to 9. One character stays one character, so positions still line up."""
    odd = {ch for ch in set(text) if ch.isdigit() and not ch.isascii()}
    if not odd:
        return text
    table = {}
    for ch in odd:
        value = unicodedata.digit(ch, None)
        if value is not None:
            table[ord(ch)] = str(value)
    return text.translate(table)


# ── Claims ────────────────────────────────────────────────────────────────────
_NOT = r"(?<!\bnot )(?<!\bno )(?<!\bnever )(?<!\bwithout )(?<!n't )"
_DISEASES = (
    r"diabetes|cancer|hiv|aids|covid(?:-19)?|asthma|arthritis|hypertension|high blood pressure|blood pressure|thyroid|"
    r"depression|anxiety|piles|kidney stones?|heart disease|obesity|infertility|baldness"
)
_CLAIM_PATTERNS = [
    re.compile(_NOT + r"\b(?:guaranteed|assured|promised)\s+(?:high\s+)?(?:returns?|profits?|income|earnings?|results?|weight loss|"
               r"cure|success|growth|selection|admission|job|placement|ranking)\b", re.I),
    re.compile(_NOT + r"\b(?:risk[- ]free|zero[- ]risk|no[- ]risk)\b", re.I),
    re.compile(_NOT + r"\b100\s?%\s*(?:guaranteed|safe|secure|sure|certain|effective|natural cure|risk[- ]free)\b", re.I),
    re.compile(_NOT + r"\b(?:cures?|cured|reverses?|reversed|permanently (?:treats?|heals?)|miracle cure for)\s+(?:your\s+|the\s+)?(?:" + _DISEASES + r")\b", re.I),
    re.compile(r"\bmiracle (?:cure|pill|remedy|treatment)\b", re.I),
    re.compile(r"\bget rich quick\b", re.I),
    re.compile(r"\bdoubles? your (?:money|investment)\b", re.I),
    re.compile(r"\blose\s+\d+\s*(?:kg|kgs|pounds|lbs|inches)\s+in\s+(?:just\s+)?\d+\s+(?:days?|weeks?)\b", re.I),
    re.compile(_NOT + r"\bno side[- ]effects?\b", re.I),
    re.compile(r"\bearn\s+(?:₹|rs\.?|inr|\$)?\s?[\d,]+(?:\.\d+)?\s*(?:k|lakhs?|crores?)?\s*(?:/|per|a|every)\s*(?:day|week)\b[^.]{0,40}\b"
               r"(?:without|from home|guaranteed|easily|effortless(?:ly)?)\b", re.I),
]


def claim_hits(text: str) -> list[Hit]:
    out: list[Hit] = []
    for pattern in _CLAIM_PATTERNS:
        for m in pattern.finditer(text):
            out.append((CLAIMS, m.group(0).strip()[:60], m.span()))
    return out


# ── Personal and financial details ────────────────────────────────────────────
_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1], [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1], [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def verhoeff_ok(digits: str) -> bool:
    """The check digit Aadhaar numbers carry, so a random 12 digits is not mistaken for one."""
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


def luhn_ok(digits: str) -> bool:
    total, flip = 0, False
    for ch in reversed(digits):
        n = int(ch)
        if flip:
            n *= 2
            n -= 9 if n > 9 else 0
        total += n
        flip = not flip
    return total % 10 == 0


_AADHAAR = re.compile(r"(?<!\d)([2-9]\d{3})[\s-]?(\d{4})[\s-]?(\d{4})(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_PAN = re.compile(r"(?<![A-Za-z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Za-z0-9])")
_IFSC = re.compile(r"(?<![A-Za-z0-9])[A-Z]{4}0[A-Z0-9]{6}(?![A-Za-z0-9])")
_PHONE_IN = re.compile(r"(?<![\d+])(?:\+?91[\s-]?|0)?([6-9]\d{4}[\s-]?\d{5})(?!\d)")
_PHONE_INTL = re.compile(r"(?<![\d+])\+(?!91\b)\d{1,3}[\s-]?\d(?:[\s-]?\d){7,11}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,})")
_URL_HOST = re.compile(r"""(?:https?://)?(?:www\.)?([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,})(?=[/:?#\s"'),;<>.]|$)""", re.I)


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", value)


def contact_keys(text: str) -> set[str]:
    """Phone numbers, emails and website names found in a text, in a form that can be compared: `tel:9876543210`, `mail:a@b.com`
    and `dom:b.com`. Used on a brand's own details, so what it writes about itself is never taken for someone else's."""
    t = ascii_digits(text or "")
    keys: set[str] = set()
    for m in _PHONE_IN.finditer(t):
        keys.add("tel:" + _digits_only(m.group(1))[-10:])
    for m in _PHONE_INTL.finditer(t):
        keys.add("tel:" + _digits_only(m.group(0))[-10:])
    for m in _EMAIL.finditer(t):
        keys.add("mail:" + m.group(0).lower())
        keys.add("dom:" + m.group(1).lower())
    for m in _URL_HOST.finditer(t):
        keys.add("dom:" + m.group(1).lower().removeprefix("www."))
    return keys


def personal_hits(text: str, own: Iterable[str] = ()) -> list[Hit]:
    """Details that identify a person or open an account. Aadhaar, PAN, card and bank codes are always found; a phone number or
    email is found unless it is one of the brand's `own` contact keys."""
    own_keys = set(own or ())
    t = ascii_digits(text)
    out: list[Hit] = []
    taken: list[tuple[int, int]] = []

    def free(span: tuple[int, int]) -> bool:
        return not any(a < span[1] and span[0] < b for a, b in taken)

    for m in _AADHAAR.finditer(t):
        digits = _digits_only(m.group(0))
        if len(digits) == 12 and verhoeff_ok(digits):
            out.append((PERSONAL, "Aadhaar number", m.span()))
            taken.append(m.span())
    for m in _CARD.finditer(t):
        digits = _digits_only(m.group(0))
        if 13 <= len(digits) <= 19 and luhn_ok(digits) and free(m.span()):
            out.append((PERSONAL, "card number", m.span()))
            taken.append(m.span())
    for m in _PAN.finditer(t):
        out.append((PERSONAL, "PAN", m.span()))
        taken.append(m.span())
    for m in _IFSC.finditer(t):
        out.append((PERSONAL, "bank code (IFSC)", m.span()))
        taken.append(m.span())
    for pattern in (_PHONE_IN, _PHONE_INTL):
        for m in pattern.finditer(t):
            if not free(m.span()):
                continue
            number = _digits_only(m.group(1) if pattern is _PHONE_IN else m.group(0))[-10:]
            taken.append(m.span())
            if "tel:" + number not in own_keys:
                out.append((PERSONAL, "phone number", m.span()))
    for m in _EMAIL.finditer(t):
        address, domain = m.group(0).lower(), m.group(1).lower()
        if "mail:" + address in own_keys or "dom:" + domain in own_keys:
            continue
        out.append((PERSONAL, "email address", m.span()))
    return out


def sensitive_hits(text: str, own: Iterable[str] = ()) -> list[Hit]:
    return claim_hits(text) + personal_hits(text, own)


def mask_personal(text: str) -> str:
    """The text with personal details hidden, for anything kept for staff to read: staff see that a number was there, not the number."""
    t = ascii_digits(text)
    spans = sorted((span for _, _, span in personal_hits(t)), reverse=True)
    out = text
    for start, end in spans:
        out = out[:start] + "[hidden]" + out[end:]
    return out
