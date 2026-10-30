"""Builds the sample data for the Ops prompt library screens from the real repository.

Writes two JSON files into the frontend (lib/prompt-library/mock/):
  inventory.generated.json   every prompt file: path, class, reason, text with region markers, code references
  ops-data.generated.json    the real register notes, content goals, tone texts and quality word lists

The region markers are what the real Phase 0 will add to the repo files. Here they are added by a
simple rule so the screens behave like the finished product:
  Dev     whole file is one locked region
  Ops     whole file is one editable region
  Hybrid  paragraphs holding template logic, answer shapes, limits or fences are locked; the rest is editable

Run from the backend folder:  .venv/Scripts/python.exe scripts/export_prompt_library_mock.py markers | manifest | mock <dir>
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "app" / "prompts"
APP = ROOT / "app"

OPS, HYBRID, DEV = "ops", "hybrid", "dev"
PLATFORMS = ["blog", "bluesky", "facebook", "instagram", "linkedin", "newsletter", "threads", "twitter", "twitter_thread", "youtube"]
CHIPS = ["add_cta", "add_hashtags", "add_keywords", "add_numbers", "add_story", "add_timestamps", "expand",
         "fix_weasel_words", "make_punchier", "more_casual", "more_formal", "shorten", "simplify_show_notes", "stronger_hook"]

CLASS: dict[str, str] = {}


def put(kind: str, names: list[str]) -> None:
    for n in names:
        assert n not in CLASS, n
        CLASS[n] = kind


put(OPS, ["analytics/analyze", "analytics/analyze_question", "supervisor/odette_system",
          "text/generate/engagement_patterns", "text/normalize/research_topic", "text/refine/system_prefix",
          "text/repurpose/fallback", "text/seo/platform_focus"])
put(OPS, [f"text/generate/cta_rules/{p}" for p in PLATFORMS])
put(OPS, [f"text/generate/hashtag_rules/{p}" for p in PLATFORMS])
put(OPS, [f"text/refine/chips/{c}" for c in CHIPS])
put(HYBRID, ["audio/fit_script", "audio/localize/translate_script", "media/shared/generate", "support/draft_reply",
             "text/generate/master", "text/generate/specificity", "text/hooks/generate", "text/hooks/score",
             "text/refine/apply_chip", "text/refine/system", "text/repurpose/master", "text/repurpose/platform_pairs"])
put(HYBRID, [f"text/generate/platform_rules/{p}" for p in PLATFORMS])
put(DEV, ["analytics/recommend", "audio/chapters", "audio/soundbite_refine", "audio/suggest_clips",
          "audio/localize/score_translation", "brand/extract_traits", "brand/preview_rewrite", "brand/watermark_suggest",
          "brand/suggest_voice_patterns", "campaigns/suggest_topics", "fixtures/_test_fixture",
          "fragments/brand_context", "fragments/json_output_contract", "fragments/retry_feedback",
          "fragments/translate_template", "media/shared/analyse", "media/shared/evaluate", "media/shared/plan",
          "media/video/chapters", "personal/align_draft", "personal/judge_drift", "presets/suggest_structure",
          "supervisor/reason_kickoff", "supervisor/scratchpad_notes", "supervisor/synth_instructions",
          "support/category", "text/angles/audio", "text/angles/generate", "text/angles/video_cuts",
          "text/angles/visual", "text/generate/approved_copy",
          "text/generate/approved_vocabulary", "text/generate/banned_words", "text/generate/language_instruction",
          "text/generate/mixed_language_instruction", "text/normalize/clean_raw_content",
          "text/normalize/content_brief_format", "text/normalize/extract_content_brief",
          "text/orchestrate/batch_angles", "text/repurpose/structured", "text/repurpose/suggest", "text/seo/master"])

# Moved out of Python code into prompt files (Stage 5, Phase 0).
put(OPS, ["personal/align_system"])
put(HYBRID, ["search/agentic_system", "support/assistant_system", "publish/youtube_metadata"])
put(DEV, ["content_guard/screen", "content_guard/screen_system", "content_guard/rewrite", "content_guard/rewrite_system",
          "content_guard/picture_unsafe", "content_guard/picture_writing", "brand/extract_traits_system",
          "brand/preview_rewrite_system", "brand/suggest_voice_patterns_system", "suggest/complete_system",
          "fragments/json_repair"])

# Classified 2026-10-27 so the library shows every prompt (drafts for the owner's sign-off).
put(OPS, ['media/image_raw_style_default'])
put(HYBRID, ['media/image_raw_topic', 'media/image_raw_style', 'media/image_raw_colors', 'media/image_headline', 'media/image_pack_variation', 'media/image_polish', 'media/image_mascot_intro', 'media/image_mascot_about', 'media/image_mascot_voice', 'media/image_mascot_colors', 'media/image_mascot_background', 'video/script_from_text', 'video/script_from_images', 'video/outline', 'video/hook_writer', 'video/cover_text', 'video/title_description_tags', 'text/generate/carousel_format'])
put(DEV, ['media/image_no_text', 'media/image_soft_screen', 'media/image_screen_suffix', 'media/image_avoid', 'media/image_describe', 'media/image_qa', 'media/image_safety', 'media/image_brand_fit', 'video/edit_plan', 'video/moments', 'video/understand_window', 'video/broll_queries', 'video/filler_judge', 'video/caption_polish', 'video/qa_critic', 'video/repair', 'video/dub_translate', 'video/remy_voice_check', 'text/generate/hashtag_banned', 'text/generate/hashtag_none', 'text/generate/english_terms', 'text/critique/judge', 'text/reader/review'])

WHY = {
    "media/image_raw_topic": "How a topic becomes a picture idea is editable. The variable slots are locked.",
    "media/image_raw_style": "Style wording is editable. The variable slots are locked.",
    "media/image_raw_style_default": "The default picture style. Wording only.",
    "media/image_raw_colors": "Colour wording is editable. The colour slots are locked.",
    "media/image_headline": "How a headline is written is editable. Its length limit is locked.",
    "media/image_pack_variation": "How pictures in a pack differ is editable. The variation slot is locked.",
    "media/image_polish": "How a picture prompt is polished is editable. The no-writing rule is locked.",
    "media/image_mascot_intro": "Mascot wording is editable. The brand slots are locked.",
    "media/image_mascot_about": "Mascot wording is editable. The brand slots are locked.",
    "media/image_mascot_voice": "Mascot wording is editable. The brand slots are locked.",
    "media/image_mascot_colors": "Mascot wording is editable. The colour slots are locked.",
    "media/image_mascot_background": "Mascot wording is editable. The background rule is locked.",
    "media/image_no_text": "Keeps writing out of pictures. Tests assert its wording.",
    "media/image_soft_screen": "Replaces screens with soft light so no fake text appears.",
    "media/image_screen_suffix": "Keeps screens in pictures blank. Tests assert its wording.",
    "media/image_avoid": "Adds what a picture must avoid. Its shape is read by the code.",
    "media/image_describe": "Describes an uploaded picture for search and alt text. Short reply the code stores.",
    "media/image_qa": "Picture quality gate. Returns a verdict the code reads.",
    "media/image_safety": "Picture safety gate. Returns a verdict the code reads.",
    "media/image_brand_fit": "Brand fit gate. Returns a verdict the code reads.",
    "video/script_from_text": "How a script is written is editable. The scene format the code reads is locked.",
    "video/script_from_images": "How a script is written is editable. The scene format the code reads is locked.",
    "video/outline": "How an outline is written is editable. The section format is locked.",
    "video/hook_writer": "Hook craft is editable. The answer shape is locked.",
    "video/cover_text": "Cover wording is editable. The length limit is locked.",
    "video/title_description_tags": "Writing guidance is editable. The fields the code reads are locked.",
    "video/edit_plan": "Returns an edit plan as JSON with frame times.",
    "video/moments": "Returns moments as JSON with exact times.",
    "video/understand_window": "Reads a stretch of video. Returns JSON the code reads.",
    "video/broll_queries": "Returns stock search terms the code sends on.",
    "video/filler_judge": "Decides which words are filler. Returns JSON with times.",
    "video/caption_polish": "Polishes captions while keeping their timing. Shape is locked.",
    "video/qa_critic": "Video quality check. Returns a verdict the code reads.",
    "video/repair": "Fixes a plan that failed a check. Returns JSON the code reads.",
    "video/dub_translate": "Translation for dubbing. Timing and placeholders are checked by the code.",
    "video/remy_voice_check": "Checks a script against the brand voice. Returns a verdict.",
    "text/generate/carousel_format": "Carousel writing guidance is editable. The slide format the code reads is locked.",
    "text/generate/hashtag_banned": "Hashtags a post must not use. Its wording is checked by the code.",
    "text/generate/hashtag_none": "Tells a platform with no hashtags to use none. Tests assert its wording.",
    "text/generate/english_terms": "Language safety for kept English terms. Tests assert its wording.",
    "text/critique/judge": "Reviews a long draft and sends weak ones back. Returns a verdict the code reads.",
    "text/reader/review": "A reader's check of a draft. Returns notes the code reads.",
    "personal/align_system": "One line that sets the persona for keeping a draft in its author's voice. Wording only.",
    "search/agentic_system": "How matches are picked and explained is editable. The short reason length the screen shows is locked.",
    "support/assistant_system": "Tone and answer style are editable. Never promising fixes, no links and only known facts are locked.",
    "publish/youtube_metadata": "Writing guidance is editable. The two-line reply the code reads and the material fences are locked.",
    "content_guard/screen": "Safety check. Returns JSON with fixed categories the code reads.",
    "content_guard/screen_system": "Safety reviewer persona. Locked so a safety rule cannot be edited away.",
    "content_guard/rewrite": "Safety rewrite. Locked so a safety rule cannot be edited away.",
    "content_guard/rewrite_system": "Safety rewrite persona. Locked with the rewrite.",
    "content_guard/picture_unsafe": "Picture safety check. Returns JSON with fixed categories.",
    "content_guard/picture_writing": "Checks a picture for writing. Returns has_text as JSON.",
    "brand/extract_traits_system": "System line for a JSON task.",
    "brand/preview_rewrite_system": "System line for a JSON task.",
    "brand/suggest_voice_patterns_system": "System line for a JSON task.",
    "suggest/complete_system": "Typing suggestions. The word limit and reply shape are read by the code that cleans the reply.",
    "fragments/json_repair": "The retry when an answer is not valid JSON.",
    "analytics/analyze": "Free text read by people.",
    "analytics/analyze_question": "Free text read by people.",
    "supervisor/odette_system": "Odette's voice and manner. Wording only.",
    "text/generate/engagement_patterns": "Craft guidance for hooks. Wording only.",
    "text/normalize/research_topic": "Turns a topic into a brief. Free text.",
    "text/refine/system_prefix": "Style rules for the refine chat.",
    "text/repurpose/fallback": "One line of guidance.",
    "text/seo/platform_focus": "Per-platform SEO emphasis.",
    "audio/fit_script": "Wording is editable. The fence, the target length numbers and the return-only rule are locked.",
    "audio/localize/translate_script": "Translation style is editable. The script fence and the return-only rule are locked.",
    "media/shared/generate": "How to write from a transcript is editable. The transcript fences are locked.",
    "support/draft_reply": "Voice and do and don't list are editable. The ticket fence, the trust line and never promising fixes are locked.",
    "text/generate/master": "Writing craft rules are editable. The answer shape, content fence, language lines and variable slots are locked.",
    "text/generate/specificity": "Examples and rules are editable. Variable slots are locked.",
    "text/hooks/generate": "The hook approaches are editable. The answer shape and the fence are locked.",
    "text/hooks/score": "Scoring guidance is editable. The answer shape and score ranges are locked.",
    "text/refine/apply_chip": "General rules are editable. The two fences and the return-only line are locked.",
    "text/refine/system": "The chat persona is editable. The fence and safety lines are locked.",
    "text/repurpose/master": "How to adapt is editable. The answer shape and the source fence are locked.",
    "text/repurpose/platform_pairs": "Advice per platform pair is editable. The pair keys the code looks up are locked.",
    "analytics/recommend": "Returns a list the code reads.",
    "audio/chapters": "Returns chapters as JSON, checked against the recording.",
    "audio/soundbite_refine": "Returns one action from a fixed list.",
    "audio/suggest_clips": "Returns clips as JSON with exact times.",
    "audio/localize/score_translation": "Returns one score line the code parses.",
    "brand/extract_traits": "Returns traits as JSON.",
    "brand/preview_rewrite": "Returns JSON with the rewrite and a score.",
    "brand/suggest_voice_patterns": "Returns suggestions as JSON.",
    "campaigns/suggest_topics": "Returns topics and tone as JSON.",
    "fixtures/_test_fixture": "Test only.",
    "fragments/brand_context": "517 lines, three shapes, central to every generation, logic heavy. Later: expose a few wording lines as Hybrid.",
    "fragments/json_output_contract": "The answer contract itself.",
    "fragments/retry_feedback": "Drives the retry loop. Its wording is tied to the leak markers.",
    "fragments/translate_template": "The translation wrapper. The leak and placeholder checks depend on its shape.",
    "media/shared/analyse": "Returns analysis as JSON.",
    "media/shared/evaluate": "Returns scores the code parses.",
    "media/shared/plan": "Returns a plan the code reads.",
    "media/video/chapters": "Returns chapters as JSON.",
    "personal/align_draft": "Returns JSON.",
    "personal/judge_drift": "Returns JSON with fixed severity values.",
    "presets/suggest_structure": "Returns sections as JSON.",
    "supervisor/reason_kickoff": "Internal step of the supervisor.",
    "supervisor/scratchpad_notes": "Internal step of the supervisor.",
    "supervisor/synth_instructions": "Returns insights as JSON.",
    "support/category": "Must answer with exactly one area from a list.",
    "text/angles/generate": "Returns angles as JSON.",
    "brand/watermark_suggest": "Returns short watermark texts as JSON.",
    "text/angles/audio": "Returns spoken scripts as JSON.",
    "text/angles/video_cuts": "Returns cuts as JSON with exact times.",
    "text/angles/visual": "Returns picture plans as JSON, with a look from a fixed list.",
    "text/generate/approved_copy": "Enforces the brand's required phrases, openers and closers, which code also checks.",
    "text/generate/approved_vocabulary": "Enforces the brand's vocabulary, which code also checks.",
    "text/generate/banned_words": "Enforces banned words, which code also checks.",
    "text/generate/language_instruction": "Language safety. Tests assert its wording.",
    "text/generate/mixed_language_instruction": "Mixed-language safety (Latin-letter blend). Tests assert its wording.",
    "text/normalize/clean_raw_content": "Returns cleaned text inside JSON. Length is checked.",
    "text/normalize/content_brief_format": "Formats model output into the next prompt.",
    "text/normalize/extract_content_brief": "Returns a brief as JSON.",
    "text/orchestrate/batch_angles": "Returns a list of angles as JSON.",
    "text/repurpose/structured": "Returns sections as JSON against the member's own section rules, which code checks.",
    "text/repurpose/suggest": "Returns platform suggestions as JSON from a fixed list.",
    "text/seo/master": "Returns the SEO package as JSON.",
}
PLATFORM_WHY = "Platform guidance is editable. The numbers the code also checks (limits, thread and tweet shape, placeholder text) are locked."

# families that the library shows as one folder summary row
SAFETY_SENSITIVE = {"support/draft_reply", "text/refine/system", "text/generate/master", "text/repurpose/master",
                    "support/assistant_system", "content_guard/screen", "content_guard/rewrite",
                    "content_guard/picture_unsafe"}


def reason(path: str) -> str:
    if path in WHY:
        return WHY[path]
    if "platform_rules/" in path:
        return PLATFORM_WHY
    if "cta_rules/" in path:
        return "One short rule about calls to action for this platform."
    if "hashtag_rules/" in path:
        return "One short rule about hashtags for this platform."
    if "refine/chips/" in path:
        return "One short instruction behind a quick-action chip."
    return ""


# ---- regions ------------------------------------------------------------------------------------
LOCK_HINTS = re.compile(r"\{\{|\}\}|\{%|%\}|\bJSON\b|^\s*[\[{]|\"[a-z_]+\" *:|</?[a-z_]+>|Return (only|ONLY|valid)", re.M)
LIMIT_HINTS = re.compile(r"\d{2,}[^\n]{0,40}(char|word|tweet|hashtag|second|limit|minimum|maximum)|(char|word|tweet|hashtag|limit|minimum|maximum)[^\n]{0,40}\d{2,}", re.I)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:28] or "section"


def label_for(paragraph: str) -> str:
    first = paragraph.strip().splitlines()[0]
    words = re.sub(r"[^A-Za-z0-9' ]+", " ", first).split()
    text = " ".join(words[:6]) or "Section"
    return text[:48]


def _wrap(kind: str, rid: str, label: str, body: str, last: bool) -> str:
    """One marked region. Every marker sits on its own line, so deleting the marker lines gives back the original file
    byte for byte. The last region of a file with no final newline has no end marker (the reader closes it)."""
    nl = chr(10)
    head = f"{{# @{kind} {rid} #}}{nl}" if kind == "locked" else f'{{# @editable {rid} "{label}" #}}{nl}'
    if last and not body.endswith(nl):
        return head + body
    return head + body + "{# @end #}" + nl


def _segments(klass: str, text: str) -> list[list[str]]:
    """The file cut into [kind, body] runs: L locked, E editable."""
    if klass == DEV:
        return [["L", text]]
    if klass == OPS:
        return [["E", text]]

    # Hybrid: lines holding template logic, answer shapes, limits or fences are locked; the prose is editable.
    runs: list[list] = []
    for line in re.findall(r"[^\n]*\n|[^\n]+$", text):
        if not line.strip():
            kind = runs[-1][0] if runs else "E"  # blank lines stay with the run before them
        else:
            kind = "L" if (LOCK_HINTS.search(line) or LIMIT_HINTS.search(line)) else "E"
        if runs and runs[-1][0] == kind:
            runs[-1][1] += line
        else:
            runs.append([kind, line])

    # an editable run too small to be worth editing is locked with its neighbours
    merged: list[list[str]] = []
    for kind, body in runs:
        if kind == "E" and len(body.strip()) < 40:
            kind = "L"
        if merged and merged[-1][0] == kind:
            merged[-1][1] += body
        else:
            merged.append([kind, body])
    return merged


def _inside_tag(text: str) -> bool:
    """True when the text ends inside an open `{% %}`, `{{ }}` or `{# #}` (a tag that spans lines)."""
    opened = max(text.rfind(t) for t in ("{%", "{{", "{#"))
    closed = max(text.rfind(t) for t in ("%}", "}}", "#}"))
    return opened > closed


_TRIM_AFTER = re.compile(r"-(%|\}|#)\}\s*$")
_TRIM_BEFORE = re.compile(r"^\s*\{(%|\{|#)-")


def _join_trimming(segments: list[list[str]]) -> list[list[str]]:
    """A marker line between a tag that trims whitespace (`-%}`, `{%-`) and its neighbour would change what is trimmed, so
    those two runs become one (locked when either was locked)."""
    out: list[list[str]] = []
    for kind, body in segments:
        if out and (_TRIM_AFTER.search(out[-1][1]) or _TRIM_BEFORE.search(body) or _inside_tag("".join(b for _, b in out))):
            out[-1] = ["L" if "L" in (out[-1][0], kind) else "E", out[-1][1] + body]
        else:
            out.append([kind, body])
    return out


def mark(path: str, klass: str, text: str) -> str:
    """The file with region markers. A file that already has them is returned as it is."""
    from app.prompt_library.regions import has_markers

    if has_markers(text):
        return text
    segments = _join_trimming(_segments(klass, text))
    if klass == HYBRID and all(kind == "L" for kind, _ in segments):
        # A prompt too short to split (often one line): all of it is editable, and the parts the code depends on are
        # protected text instead (see protected_literals).
        segments = [["E", text]]
    out: list[str] = []
    used: set[str] = set()
    n_locked = 0
    for i, (kind, body) in enumerate(segments):
        last = i == len(segments) - 1
        if kind == "L":
            n_locked += 1
            rid = "whole" if klass == DEV else f"locked_{n_locked}"
            out.append(_wrap("locked", rid, "", body, last))
            continue
        label = "Prompt text" if klass == OPS else label_for(body)
        rid = "all" if klass == OPS else slug(label)
        while rid in used:
            rid += "_2"
        used.add(rid)
        out.append(_wrap("editable", rid, label, body, last))
    return "".join(out)


# Exact text an edit must keep, beyond what the checks already protect (variables, template tags, fences).
PROTECT: dict[str, list[str]] = {
    "media/image_polish": [
        "The image must contain NO text of any kind: no words, letters, numbers, logos, brand names, captions, signs, "
        "labels, or screens, posters, books or devices showing writing.",
        "Reply with ONLY the rewritten prompt, no preamble, under 400 characters.",
    ],
    "text/generate/carousel_format": ['"Slide 1:", "Slide 2:"'],
}

FENCE = re.compile(r"\{\{-?[^{}]*\|\s*untrusted\([^{}]*\)[^{}]*-?\}\}")


def protected_literals(path: str, klass: str, raw: str, marked: str) -> list[str]:
    """Text an edit must keep: placeholders members replace ([LIKE THIS]), every untrusted-text fence, the extra rules
    above, and for a prompt that is one editable section, its limits and answer-shape phrases."""
    from app.prompt_library.regions import parse_regions

    found = set(re.findall(r"\[[A-Z][A-Z ]{3,}\]", raw))
    found.update(m.group(0) for m in FENCE.finditer(raw))
    found.update(PROTECT.get(path, []))
    regions = parse_regions(marked)
    if klass == HYBRID and len(regions) == 1 and regions[0].kind == "editable":
        found.update(m.group(0).strip() for m in LIMIT_HINTS.finditer(raw))
        found.update(m.group(0) for m in re.finditer(r"Return (only|ONLY|valid)[^.\n]*\.|Reply with ONLY[^.\n]*\.", raw))
    return sorted(t for t in found if t and t in raw)


# ---- code references ---------------------------------------------------------------------------
CALL = re.compile(r"(?:load_prompt|render_fragment)\(\s*(?:\n\s*)?[\"']([^\"']+)[\"']")


def code_references() -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}
    prefix_hits: dict[str, list[str]] = {}
    for py in APP.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        src = py.read_text(encoding="utf-8", errors="ignore")
        rel = py.relative_to(ROOT).as_posix()
        for m in CALL.finditer(src):
            refs.setdefault(m.group(1), [])
            if rel not in refs[m.group(1)]:
                refs[m.group(1)].append(rel)
        for m in re.finditer(r"[\"'](text/[a-z_/]+/)[\"'{]", src):
            prefix_hits.setdefault(m.group(1), [])
            if rel not in prefix_hits[m.group(1)]:
                prefix_hits[m.group(1)].append(rel)
    return refs | {"__prefix__": prefix_hits}  # type: ignore[dict-item]


def _call_name(node: ast.Call) -> str:
    f = node.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def call_variables(found: list[str]) -> dict[str, dict[str, dict]]:
    """The variables the code really passes to each prompt: the keyword arguments at every load_prompt call.

    Returns {prompt path: {variable name: {"source": code that supplies it, "sites": number of call sites}}}.
    A call that spreads a dictionary (**values) is recorded under the special name "**" because those names
    are not visible in the source. A call whose path is built with an f-string is matched by its fixed start.
    """
    out: dict[str, dict[str, dict]] = {}

    def add(path: str, name: str, source: str) -> None:
        slot = out.setdefault(path, {}).setdefault(name, {"source": source, "sites": 0})
        slot["sites"] += 1

    for py in APP.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in ("load_prompt", "render_fragment") or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                targets = [first.value] if first.value in found else []
            elif isinstance(first, ast.JoinedStr) and first.values and isinstance(first.values[0], ast.Constant):
                start = str(first.values[0].value)
                targets = [f for f in found if f.startswith(start)]
            else:
                targets = []
            for target in targets:
                for kw in node.keywords:
                    if kw.arg is None:
                        add(target, "**", ast.unparse(kw.value)[:70])
                    else:
                        add(target, kw.arg, ast.unparse(kw.value)[:70])
    return out


def prompt_paths() -> list[str]:
    found = sorted(p.relative_to(PROMPTS).as_posix()[:-6] for p in PROMPTS.rglob("*.jinja"))
    assert sorted(CLASS) == found, (set(CLASS) ^ set(found))
    return found


def _read(path: str) -> str:
    return (PROMPTS / f"{path}.jinja").read_text(encoding="utf-8").replace("\r\n", "\n")


def apply_markers() -> None:
    """Writes the region markers into every prompt file that has none, after proving for each one that the marked file
    gives back the original byte for byte and compiles to the same template. Nothing is written if any file fails."""
    sys.path.insert(0, str(ROOT))
    from app.prompt_library.regions import compose, parse_regions, strip_markers, token_stream  # noqa: WPS433
    from app.prompts.registry import _env  # noqa: WPS433

    pending: dict[str, str] = {}
    failed: list[str] = []
    for path in prompt_paths():
        raw = _read(path)
        marked = mark(path, CLASS[path], raw)
        if marked == raw:
            continue
        same = strip_markers(marked) == raw and compose(parse_regions(marked)) == raw
        if not same or token_stream(_env, marked) != token_stream(_env, raw):
            failed.append(path)
        pending[path] = marked
    if failed:
        raise SystemExit(f"not written, these would change: {failed}")
    for path, marked in pending.items():
        (PROMPTS / f"{path}.jinja").write_bytes(marked.encode("utf-8"))
    print("marked", len(pending), "prompt files")


def build_items() -> list[dict]:
    """Every prompt file as the library sees it: class, reason, sections, protected text and what the code passes in."""
    sys.path.insert(0, str(ROOT))
    from app.prompt_library.regions import strip_markers  # noqa: WPS433

    found = prompt_paths()
    refs = code_references()
    prefix = refs.pop("__prefix__")  # type: ignore[arg-type]
    variables = call_variables(found)
    items = []
    for path in found:
        klass = CLASS[path]
        text = mark(path, klass, _read(path))
        raw = strip_markers(text)
        used_by = refs.get(path) or next((files for pre, files in prefix.items() if path.startswith(pre)), [])
        items.append({
            "path": path,
            "folder": path.rsplit("/", 1)[0] if "/" in path else "",
            "name": path.rsplit("/", 1)[-1],
            "klass": klass,
            "reason": reason(path),
            "safetySensitive": path in SAFETY_SENSITIVE,
            "lines": len(raw.splitlines()),
            "text": text,
            "usedBy": sorted(used_by),
            "protectedLiterals": protected_literals(path, klass, raw, text),
            "receives": [
                {"name": n, "source": v["source"], "sites": v["sites"]}
                for n, v in sorted(variables.get(path, {}).items())
                if n != "**"
            ],
            "receivesMore": "**" in variables.get(path, {}),
        })
    return items


MANIFEST = PROMPTS / "manifest.json"
MANIFEST_KEYS = ("klass", "reason", "safetySensitive", "usedBy", "protectedLiterals", "receives", "receivesMore")


def manifest_of(items: list[dict]) -> dict:
    """What the server needs about each prompt that is not in the file itself."""
    return {"prompts": {i["path"]: {k: i[k] for k in MANIFEST_KEYS} for i in items}}


def manifest_text(items: list[dict]) -> str:
    return json.dumps(manifest_of(items), ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def write_manifest(items: list[dict]) -> None:
    MANIFEST.write_bytes(manifest_text(items).encode("utf-8"))
    print("wrote", MANIFEST.relative_to(ROOT).as_posix(), "with", len(items), "prompts")


def write_mock(out_dir: Path, items: list[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "inventory.generated.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def write_ops_data(out_dir: Path) -> None:
    """The real values the Ops data lists start from (the same ones the server shows as the code values)."""
    sys.path.insert(0, str(ROOT))
    from app.prompt_library.ops_lists import code_defaults  # noqa: WPS433

    data = {
        "registerNotes": code_defaults("register_notes"),
        "toneOptions": code_defaults("tone_options"),
        "contentGoals": code_defaults("content_goals"),
        "qualityWordLists": code_defaults("quality_words"),
    }
    (out_dir / "ops-data.generated.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote Ops data", {k: len(v) for k, v in data.items()})


if __name__ == "__main__":
    # markers            put the region markers into the prompt files (proved unchanged first)
    # manifest           write app/prompts/manifest.json
    # mock <frontend dir> write the sample data for the Ops screens
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "markers":
        apply_markers()
    elif command == "manifest":
        write_manifest(build_items())
    elif command == "mock" and len(sys.argv) > 2:
        built = build_items()
        write_mock(Path(sys.argv[2]), built)
        write_ops_data(Path(sys.argv[2]))
        counts = {k: sum(1 for i in built if i["klass"] == k) for k in (OPS, HYBRID, DEV)}
        print("wrote", len(built), "prompts", counts)
    else:
        raise SystemExit("usage: export_prompt_library_mock.py markers | manifest | mock <frontend mock dir>")
