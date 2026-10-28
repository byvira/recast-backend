"""The small lists the product reads as plain data, which Ops can edit: language register notes, tone options, content
goals and the quality word lists.

Each reader returns the published Ops list when one is live (app/prompt_library/live.py), and otherwise exactly the values
in the code, which stay the fallback. `code_defaults()` is what the Ops screens show as "the code values".
"""

from __future__ import annotations

from typing import Any

from app.prompt_library import live

KINDS = ("register_notes", "tone_options", "content_goals", "quality_words")

# The names the tone picker shows for each tone. "punchy" is the picker's Bold button.
TONE_LABELS = {
    "formal": "Formal",
    "casual": "Casual",
    "punchy": "Bold",
    "storytelling": "Storytelling",
    "professional": "Professional",
    "direct": "Direct",
    "witty": "Witty",
    "empathetic": "Empathetic",
}

# The quality word lists: (language, list id) -> label. The words themselves are in app/pipelines/text/generator.py.
WORD_LISTS = [
    ("en", "generic_openings", "Generic openings"),
    ("en", "generic_closings", "Generic closings"),
    ("en", "filler_words", "Filler words"),
]


def _text_fixture() -> dict:
    from app.prompts.registry import load_fixture

    return load_fixture("brand_context_text")


def _code_words(list_id: str) -> list[str]:
    from app.pipelines.text import generator

    return list({"generic_openings": generator.GENERIC_OPENINGS, "generic_closings": generator.GENERIC_CLOSINGS,
                 "filler_words": generator.FILLER_WORDS}[list_id])


def code_defaults(kind: str) -> list[dict[str, Any]]:
    """The values in the code for one list, in the shape the Ops screens edit."""
    if kind == "register_notes":
        from app.models.text import LANGUAGE_NAMES
        from app.pipelines.text.generator import CONVERSATIONAL_REGISTER_NOTES

        return [{"code": code, "language": LANGUAGE_NAMES.get(code, code), "note": CONVERSATIONAL_REGISTER_NOTES.get(code, "")}
                for code in sorted(LANGUAGE_NAMES)]
    if kind == "tone_options":
        tones = _text_fixture()["tone_instructions"]
        return [{"id": tid, "label": TONE_LABELS.get(tid, tid.capitalize()), "instruction": text, "enabled": True}
                for tid, text in tones.items()]
    if kind == "content_goals":
        return [{"id": gid, "text": text} for gid, text in _text_fixture()["goal_instructions"].items()]
    if kind == "quality_words":
        return [{"language": lang, "list": lid, "label": label, "words": _code_words(lid)} for lang, lid, label in WORD_LISTS]
    raise KeyError(kind)


# ---------------------------------------------------------------------------------------------------------------------
# What the product reads
# ---------------------------------------------------------------------------------------------------------------------

def register_note(code: str) -> str:
    """The register note for a language code ("ta"), or "" when it has none."""
    rows = live.ops_data("register_notes")
    if rows is None:
        from app.pipelines.text.generator import CONVERSATIONAL_REGISTER_NOTES

        return CONVERSATIONAL_REGISTER_NOTES.get(code, "")
    return next((str(r.get("note") or "") for r in rows if r.get("code") == code), "")


def tone_instructions() -> dict[str, str]:
    """Each tone's wording: the published Ops text where there is one. A tone removed in Ops keeps its code wording, so a
    member who already chose it never silently gets no tone at all."""
    code = dict(_text_fixture()["tone_instructions"])
    rows = live.ops_data("tone_options")
    if rows is None:
        return code
    return {**code, **{str(r["id"]): str(r.get("instruction") or "") for r in rows if r.get("id")}}


def shown_tones() -> list[str]:
    """The tones the member picker may show (switched on in Ops), in the Ops order. Every tone with wording when nothing
    is published."""
    rows = live.ops_data("tone_options")
    if rows is None:
        return list(_text_fixture()["tone_instructions"])
    return [str(r["id"]) for r in rows if r.get("id") and r.get("enabled", True)]


def goal_instructions() -> dict[str, str]:
    rows = live.ops_data("content_goals")
    if rows is None:
        return dict(_text_fixture()["goal_instructions"])
    return {str(r["id"]): str(r.get("text") or "") for r in rows if r.get("id")}


def words(list_id: str, language: str = "en") -> list[str]:
    """One quality word list: the published one when live, else the code's."""
    rows = live.ops_data("quality_words")
    if rows is not None:
        match = next((r for r in rows if r.get("list") == list_id and r.get("language", "en") == language), None)
        if match is not None:
            return [str(w) for w in match.get("words") or []]
    return _code_words(list_id) if language == "en" else []
