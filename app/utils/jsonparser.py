"""
llm_parser.py — Robust JSON parsing utility for LLM responses.

Handles the most common LLM output issues:
  - Extra text / explanation after the JSON object
  - Markdown fences (```json ... ```)
  - Embedded control characters (\n inside strings)
  - Two JSON objects concatenated ("Extra data" error)
  - Truncated / incomplete JSON

Usage:
    from app.shared.llm_parser import parse_llm_json, extract_first_json
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def parse_llm_json(raw: str) -> dict[str, Any] | list[Any]:
    """
    Parse a raw LLM response into a Python dict or list.

    Parsing cascade (stops at the first success):
      1. Strip markdown fences, then strict json.loads
      2. Extract the first balanced JSON object/array, then strict parse
      3. Same extraction, lenient parse (strict=False — allows control chars)
      4. Same extraction, escape-and-retry (sanitises remaining control chars)
      5. Log and return {} on total failure — never raises

    Returns:
        Parsed dict or list.  Empty dict {} on failure.
    """
    if not raw or not raw.strip():
        logger.warning("parse_llm_json: empty input")
        return {}

    # --- pass 1: strip fences, try direct parse --------------------------
    cleaned = _strip_fences(raw)
    result = _try_loads(cleaned, label="direct")
    if result is not None:
        return result

    # --- pass 2-4: extract first balanced object, then try parse cascade --
    fragment = extract_first_json(cleaned)
    if not fragment:
        logger.error(
            "parse_llm_json: no JSON object found. Raw (500 chars): %s",
            raw[:500],
        )
        return {}

    result = _try_loads(fragment, label="strict")
    if result is not None:
        return result

    result = _try_loads(fragment, label="lenient", strict=False)
    if result is not None:
        return result

    sanitised = _escape_control_chars(fragment)
    result = _try_loads(sanitised, label="escape-retry")
    if result is not None:
        return result

    logger.error(
        "parse_llm_json: all passes failed. Fragment (500 chars): %s",
        fragment[:500],
    )
    return {}


def extract_first_json(text: str) -> str | None:
    """
    Walk `text` character-by-character and return the first complete,
    balanced JSON object { } or array [ ].

    Why not regex?  re.search(r'\{.*\}', s, re.DOTALL) is greedy — it
    matches from the first '{' to the LAST '}', so two concatenated JSON
    objects produce "Extra data" on parse.  This function stops at the
    exact closing bracket of the first structure.

    Returns:
        The extracted JSON string, or None if no balanced structure found.
    """
    start: int | None = None
    close_char: str | None = None
    depth = 0
    in_string = False
    escape_next = False

    for i, ch in enumerate(text):
        # handle backslash escapes inside strings
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        # toggle string context
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue

        # structural characters
        if ch in ("{", "["):
            if depth == 0:
                start = i
                close_char = "}" if ch == "{" else "]"
            depth += 1
        elif ch in ("}", "]"):
            if depth > 0:
                depth -= 1
                if depth == 0 and ch == close_char:
                    return text[start : i + 1]

    return None


# ─────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` markdown fences."""
    text = re.sub(r"```(?:json|JSON)?", "", text)
    text = text.replace("```", "")
    return text.strip()


def _try_loads(
    text: str,
    *,
    label: str,
    strict: bool = True,
) -> dict[str, Any] | list[Any] | None:
    """
    Attempt json.loads and return the result, or None on failure.
    Logs only at DEBUG level so hot-path noise stays low.
    """
    try:
        return json.loads(text) if strict else json.loads(text, strict=False)
    except json.JSONDecodeError as exc:
        logger.debug("parse pass '%s' failed: %s", label, exc)
        return None


def _escape_control_chars(text: str) -> str:
    """
    Replace raw control characters (0x00–0x1F, 0x7F) with their JSON
    escape sequences.  Skips characters that are already escaped so
    legitimate \\n sequences inside strings are not double-escaped.
    """
    # Only replace bare control characters, not already-escaped sequences.
    return re.sub(
        r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]',
        lambda m: repr(m.group())[1:-1],  # e.g. chr(7) → '\\x07'
        text,
    )