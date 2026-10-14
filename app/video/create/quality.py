"""The prompt quality program: a fixed corpus, a score per prompt and language, and a baseline that blocks a drop.

The corpus is built from five topics per language at four lengths (20 transcripts per language, from about 30 words up to the
whole set). Each prompt is run over every transcript and scored on five things, each 0 to 1 as the share of transcripts that pass:

- grounding: the answer holds no figure or claim that is not in the transcript (the validators' own checks)
- language: the answer is in the language that was asked for
- length fit: opening lines, scripts and captions are inside their length limits
- platform fit: titles, descriptions and tags are inside the platform's limits
- time validity: every edit time is inside the video and on a real silence

`score` works with any caller, so the same code scores a recorded set of answers, a stub in tests, or a live model.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from app.video.create import generate as G
from app.video.create import validate as V

Caller = Callable[[str, str], Awaitable[dict[str, Any]]]          # (prompt name, prompt text) -> answer

CORPUS_PATH = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "video_quality" / "topics.json"
BASELINE_PATH = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "video_quality" / "baseline.json"
PROMPTS = ("hook_writer", "script_from_text", "title_description_tags", "caption_polish", "edit_plan")
METRICS = ("grounding", "language", "length_fit", "platform_fit", "time_validity")
LENGTHS = (1, 2, 3, 5)                       # how many topics a transcript joins


def load_corpus(path: Optional[Path] = None) -> dict[str, list[dict[str, Any]]]:
    """{language: [{"id", "text", "words"}]}: 20 transcripts per language."""
    topics = json.loads((path or CORPUS_PATH).read_text(encoding="utf-8"))
    out: dict[str, list[dict[str, Any]]] = {}
    for lang, paragraphs in topics.items():
        rows = []
        for i in range(len(paragraphs)):
            for n in LENGTHS:
                text = " ".join(paragraphs[(i + k) % len(paragraphs)] for k in range(n))
                words = [{"w": w, "s_ms": j * 380, "e_ms": j * 380 + 330} for j, w in enumerate(text.split())]
                rows.append({"id": f"{lang}-{i + 1}-{n}", "text": text, "words": words})
        out[lang] = rows
    return out


def _words_hint(text: str) -> int:
    return len(text.split())


async def _score_one(prompt: str, lang: str, item: dict[str, Any], caller: Caller) -> dict[str, Optional[bool]]:
    """One transcript through one prompt: pass or fail for each metric that applies (None when it does not)."""
    text, words = item["text"], item["words"]
    result: dict[str, Optional[bool]] = {m: None for m in METRICS}
    holder: dict[str, str] = {}

    async def call(p: str) -> dict[str, Any]:
        holder.setdefault("p", p)
        return await caller(prompt, p)

    voice = {"context": "", "banned": []}
    if prompt == "hook_writer":
        out, meta = await G.hooks(text, language=lang, voice=voice, caller=call)
        result["grounding"] = bool(out) and not any("not in the clip" in p or "says nothing" in p for p in meta["problems"])
        result["length_fit"] = bool(out) and not any("words or fewer" in p for p in meta["problems"])
        result["language"] = meta["language_problem"] is None and bool(out)
    elif prompt == "script_from_text":
        target = max(15.0, min(60.0, _words_hint(text) / 2.5))
        out, meta = await G.script_from_text(text, target_seconds=target, language=lang, voice=voice, caller=call)
        result["grounding"] = bool(out["beats"]) and not any("not in the source" in p for p in meta["problems"])
        result["length_fit"] = bool(out["beats"]) and not any("between" in p for p in meta["problems"])
        result["language"] = meta["language_problem"] is None and bool(out["beats"])
    elif prompt == "title_description_tags":
        out, meta = await G.publish_text(text, "youtube", chapters=[], language=lang, voice=voice, caller=call)
        result["platform_fit"] = bool(out["title"]) and not any("limit" in p for p in meta["problems"])
        result["grounding"] = bool(out["title"]) and not any("times that are not" in p for p in meta["problems"])
        result["language"] = meta["language_problem"] is None and bool(out["title"])
    elif prompt == "caption_polish":
        part = words[:40]
        out, meta = await G.polish_captions(part, language=lang, glossary=[], caller=call)
        result["length_fit"] = not any("exactly" in p for p in meta["problems"]) and all(len(ln) <= 7 for ln in out["lines"])
        result["grounding"] = not any("different word" in p for p in meta["problems"])
        result["language"] = meta["language_problem"] is None
    elif prompt == "edit_plan":
        duration = words[-1]["e_ms"] + 1500
        from app.video.analysis import silence as S

        pauses = [(p["start_ms"], p["end_ms"]) for p in S.pauses(words)]
        lines = "\n".join(f"{words[i]['s_ms']} {words[i]['w']}" for i in range(0, len(words), 8))
        out, meta = await G.edit_plan(lines, pauses, duration, "natural", caller=call)
        result["time_validity"] = not any("outside the video" in p or "real silence" in p for p in meta["problems"])
    return result


async def score(caller: Caller, *, languages: Optional[list[str]] = None, prompts: Optional[tuple[str, ...]] = None,
                corpus: Optional[dict[str, list[dict[str, Any]]]] = None, limit: Optional[int] = None) -> dict[str, dict[str, dict[str, Optional[float]]]]:
    """{prompt: {language: {metric: share passing, or None when the metric does not apply to that prompt}}}."""
    corpus = corpus or load_corpus()
    table: dict[str, dict[str, dict[str, Optional[float]]]] = {}
    for prompt in prompts or PROMPTS:
        table[prompt] = {}
        for lang in languages or list(corpus):
            rows = corpus[lang][:limit] if limit else corpus[lang]
            tally: dict[str, list[bool]] = {m: [] for m in METRICS}
            for item in rows:
                one = await _score_one(prompt, lang, item, caller)
                for m, ok in one.items():
                    if ok is not None:
                        tally[m].append(ok)
            table[prompt][lang] = {m: (round(sum(v) / len(v), 3) if v else None) for m, v in tally.items()}
    return table


def drops(current: dict, baseline: dict, *, tolerance: float = 0.05) -> list[str]:
    """Every place the current scores are lower than the baseline by more than the tolerance, in plain words. Empty means the
    change may go ahead."""
    out: list[str] = []
    for prompt, langs in baseline.items():
        for lang, metrics in langs.items():
            for m, was in metrics.items():
                now = ((current.get(prompt) or {}).get(lang) or {}).get(m)
                if was is None:
                    continue
                if now is None:
                    out.append(f"{prompt} / {lang} / {m}: no longer scored (was {was:.2f})")
                elif now < was - tolerance:
                    out.append(f"{prompt} / {lang} / {m}: {now:.2f}, was {was:.2f}")
    return out


#: What share of transcripts must pass, per metric. Set by what a miss costs a member: an invented figure is published by mistake, so
#: grounding is strict; a length miss only shows a "not fully checked" note.
PASS_LINES: dict[str, float] = {"grounding": 0.95, "time_validity": 0.98, "platform_fit": 0.95, "language": 0.85, "length_fit": 0.85}


def below_pass_line(table: dict, *, lines: Optional[dict[str, float]] = None) -> list[str]:
    """The prompt, language and metric combinations that score under their pass line, so they can be listed for the stronger-model
    comparison."""
    lines = lines or PASS_LINES
    out = []
    for prompt, langs in table.items():
        for lang, metrics in langs.items():
            for m, v in metrics.items():
                line = lines.get(m, 0.85)
                if v is not None and v < line:
                    out.append(f"{prompt} / {lang} / {m}: {v:.2f} (needs {line:.2f})")
    return out
