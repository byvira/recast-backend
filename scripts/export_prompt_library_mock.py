"""Builds the sample data for the Ops prompt library screens from the real repository.

Writes two JSON files into the frontend (lib/prompt-library/mock/):
  inventory.generated.json   every prompt file: path, class, reason, text with region markers, code references
  ops-data.generated.json    the real register notes, content goals, tone texts and quality word lists

The region markers are what the real Phase 0 will add to the repo files. Here they are added by a
simple rule so the screens behave like the finished product:
  Dev     whole file is one locked region
  Ops     whole file is one editable region
  Hybrid  paragraphs holding template logic, answer shapes, limits or fences are locked; the rest is editable

Run from the backend folder:  .venv/Scripts/python.exe scripts/export_prompt_library_mock.py <frontend-mock-dir>
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
PLATFORMS = ["blog", "facebook", "instagram", "linkedin", "newsletter", "twitter", "twitter_thread", "youtube"]
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
          "audio/localize/score_translation", "brand/extract_traits", "brand/preview_rewrite",
          "brand/suggest_voice_patterns", "campaigns/suggest_topics", "fixtures/_test_fixture",
          "fragments/brand_context", "fragments/json_output_contract", "fragments/retry_feedback",
          "fragments/translate_template", "media/shared/analyse", "media/shared/evaluate", "media/shared/plan",
          "media/video/chapters", "personal/align_draft", "personal/judge_drift", "presets/suggest_structure",
          "supervisor/reason_kickoff", "supervisor/scratchpad_notes", "supervisor/synth_instructions",
          "support/category", "text/angles/generate", "text/generate/approved_copy",
          "text/generate/approved_vocabulary", "text/generate/banned_words", "text/generate/language_instruction",
          "text/generate/mixed_language_instruction", "text/normalize/clean_raw_content",
          "text/normalize/content_brief_format", "text/normalize/extract_content_brief",
          "text/orchestrate/batch_angles", "text/repurpose/structured", "text/repurpose/suggest", "text/seo/master"])

WHY = {
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
SAFETY_SENSITIVE = {"support/draft_reply", "text/refine/system", "text/generate/master", "text/repurpose/master"}


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


def _wrap(kind: str, rid: str, label: str, body: str) -> str:
    """One marked region. Every marker sits on its own line, so deleting the marker lines gives back the
    original file byte for byte."""
    nl = chr(10)
    body = body if body.endswith(nl) else body + nl
    head = f"{{# @{kind} {rid} #}}{nl}" if kind == "locked" else f'{{# @editable {rid} "{label}" #}}{nl}'
    return head + body + "{# @end #}" + nl


def mark(path: str, klass: str, text: str) -> str:
    text = text if text.endswith(chr(10)) else text + chr(10)
    if klass == DEV:
        return _wrap("locked", "whole", "", text)
    if klass == OPS:
        return _wrap("editable", "all", "Prompt text", text)

    # Hybrid: lines holding template logic, answer shapes, limits or fences are locked; the prose is editable.
    lines = text.splitlines(keepends=True)
    runs: list[tuple[str, list[str]]] = []
    for line in lines:
        if not line.strip():
            kind = runs[-1][0] if runs else "E"  # blank lines stay with the run before them
        else:
            kind = "L" if (LOCK_HINTS.search(line) or LIMIT_HINTS.search(line)) else "E"
        if runs and runs[-1][0] == kind:
            runs[-1][1].append(line)
        else:
            runs.append((kind, [line]))

    # an editable run too small to be worth editing is locked with its neighbours
    merged: list[tuple[str, str]] = []
    for kind, run in runs:
        body = "".join(run)
        if kind == "E" and len(body.strip()) < 40:
            kind = "L"
        if merged and merged[-1][0] == kind:
            merged[-1] = (kind, merged[-1][1] + body)
        else:
            merged.append((kind, body))

    out: list[str] = []
    used: set[str] = set()
    n_locked = 0
    for kind, body in merged:
        if kind == "L":
            n_locked += 1
            out.append(_wrap("locked", f"locked_{n_locked}", "", body))
            continue
        label = label_for(body)
        rid = slug(label)
        while rid in used:
            rid += "_2"
        used.add(rid)
        out.append(_wrap("editable", rid, label, body))
    return "".join(out)


# ---- code references ---------------------------------------------------------------------------
CALL = re.compile(r"load_prompt\(\s*(?:\n\s*)?[\"']([^\"']+)[\"']")


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
            if not isinstance(node, ast.Call) or _call_name(node) != "load_prompt" or not node.args:
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


def main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    found = sorted(p.relative_to(PROMPTS).as_posix()[:-6] for p in PROMPTS.rglob("*.jinja"))
    assert sorted(CLASS) == found, (set(CLASS) ^ set(found))
    refs = code_references()
    prefix = refs.pop("__prefix__")  # type: ignore[arg-type]
    variables = call_variables(found)

    items = []
    for path in found:
        klass = CLASS[path]
        raw = (PROMPTS / f"{path}.jinja").read_text(encoding="utf-8")
        used_by = refs.get(path) or next((files for pre, files in prefix.items() if path.startswith(pre)), [])
        literals = sorted(set(re.findall(r"\[[A-Z][A-Z ]{3,}\]", raw)))
        items.append({
            "path": path,
            "folder": path.rsplit("/", 1)[0] if "/" in path else "",
            "name": path.rsplit("/", 1)[-1],
            "klass": klass,
            "reason": reason(path),
            "safetySensitive": path in SAFETY_SENSITIVE,
            "lines": len(raw.splitlines()),
            "text": mark(path, klass, raw),
            "usedBy": used_by,
            "protectedLiterals": literals,
            "receives": [
                {"name": n, "source": v["source"], "sites": v["sites"]}
                for n, v in sorted(variables.get(path, {}).items())
                if n != "**"
            ],
            "receivesMore": "**" in variables.get(path, {}),
        })
    (out_dir / "inventory.generated.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- Ops data: the real values that live in code today --------------------------------------
    sys.path.insert(0, str(ROOT))
    from app.models.text import LANGUAGE_NAMES  # noqa: WPS433
    from app.pipelines.text.brand_context import build_goal_context, build_tone_override  # noqa: WPS433
    from app.pipelines.text.generator import CONVERSATIONAL_REGISTER_NOTES, GENERIC_OPENINGS  # noqa: WPS433

    goals = {}
    for goal in ["educate", "promote", "entertain", "inspire", "announce", "engage", "convert"]:
        goals[goal] = build_goal_context(goal).strip()
    tones = {}
    for tone in ["formal", "casual", "punchy", "storytelling", "professional", "direct", "witty", "empathetic"]:
        tones[tone] = build_tone_override(tone, "en").strip()

    data = {
        "registerNotes": [
            {"code": code, "language": LANGUAGE_NAMES.get(code, code), "note": CONVERSATIONAL_REGISTER_NOTES.get(code, "")}
            for code in sorted(LANGUAGE_NAMES)
        ],
        "toneOptions": [{"id": k, "label": k.capitalize(), "instruction": v, "enabled": True} for k, v in tones.items()],
        "contentGoals": [{"id": k, "text": v} for k, v in goals.items()],
        "qualityWordLists": [
            {"language": "en", "list": "generic_openings", "label": "Generic openings", "words": list(GENERIC_OPENINGS)},
            {"language": "en", "list": "generic_closings", "label": "Generic closings", "words": [
                "so, are you ready to", "the choice is yours", "what are you waiting for", "don't hesitate to",
                "join us on this journey", "let's connect", "feel free to reach out"]},
            {"language": "en", "list": "filler_words", "label": "Filler words", "words": [
                "many", "several", "often", "recently", "soon", "significant", "substantial", "various", "numerous"]},
        ],
    }
    (out_dir / "ops-data.generated.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    counts = {k: sum(1 for i in items if i["klass"] == k) for k in (OPS, HYBRID, DEV)}
    print("wrote", len(items), "prompts", counts, "and", {k: len(v) for k, v in data.items()})


if __name__ == "__main__":
    main(Path(sys.argv[1]))
