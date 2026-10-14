"""The prompt quality program: the corpus, the scoring, the drop check and the script. Offline, with stand-in answerers that behave like a
good model and a careless one."""
import json
import re
import subprocess
import sys
from pathlib import Path

from app.video.create import quality as Q
from app.video.create import validate as V

ROOT = Path(__file__).resolve().parents[1]


def _fenced(prompt: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\n(.*?)\n</{tag}>", prompt, re.S)
    return m.group(1) if m else ""


def _language(prompt: str) -> str:
    m = re.search(r"^Language: (.+)$", prompt, re.M)
    return (m.group(1).strip() if m else "en")


async def good_model(name: str, prompt: str) -> dict:
    """What a careful model would say: only words and figures from the source, the right length, the right shape."""
    lang = _language(prompt)
    if name == "hook_writer":
        return {"hooks": [{"text": " ".join(_fenced(prompt, "clip").split()[:8])}]}
    if name == "script_from_text":
        source = _fenced(prompt, "source").split()
        target = float(re.search(r"about (\d+(?:\.\d+)?) seconds", prompt).group(1))
        out: list[str] = []
        i = 0
        while V.spoken_seconds(" ".join(out), lang) < target - 0.5 and i < 4000:
            out.append(source[i % len(source)])
            i += 1
        beats = [" ".join(out[k:k + 25]) for k in range(0, len(out), 25)]
        return {"beats": [{"narration": b, "on_screen": "", "visual": "a calm desk"} for b in beats]}
    if name == "title_description_tags":
        text = _fenced(prompt, "video")
        return {"title": " ".join(text.split()[:8]), "description": text[:300], "tags": ["planning", "focus"]}
    if name == "caption_polish":
        words = [ln.split(" ", 1)[1] for ln in _fenced(prompt, "words").splitlines() if " " in ln]
        return {"words": [{"w": w} for w in words], "breaks": [], "emphasis": []}
    if name == "edit_plan":
        return {"edits": []}
    return {}


async def careless_model(name: str, prompt: str) -> dict:
    """Invents a figure in every hook and every script, and drops a word from every caption set."""
    answer = await good_model(name, prompt)
    if name == "hook_writer":
        answer["hooks"][0]["text"] += " in 99 days"
    if name == "script_from_text":
        answer["beats"][0]["narration"] += " It grew 777 percent."
    if name == "caption_polish":
        answer["words"] = answer["words"][:-1]
    return answer


def test_the_corpus_has_twenty_transcripts_per_language_in_six_languages_with_real_timing():
    corpus = Q.load_corpus()
    assert set(corpus) == {"en", "ta+en", "hi+en", "es", "fr", "de"}
    ids = set()
    for lang, rows in corpus.items():
        assert len(rows) == 20
        for r in rows:
            assert r["id"] not in ids and r["words"]
            ids.add(r["id"])
            assert all(a["e_ms"] <= b["s_ms"] for a, b in zip(r["words"], r["words"][1:]))
    lengths = sorted(len(r["words"]) for r in corpus["en"])
    assert lengths[0] < 40 and lengths[-1] > 120


async def test_a_careful_model_scores_full_marks_on_every_metric_that_applies():
    table = await Q.score(good_model)
    assert set(table) == set(Q.PROMPTS)
    for prompt, langs in table.items():
        assert set(langs) == {"en", "ta+en", "hi+en", "es", "fr", "de"}
        for lang, metrics in langs.items():
            for metric, value in metrics.items():
                assert value in (None, 1.0), (prompt, lang, metric, value)
    assert table["hook_writer"]["en"]["grounding"] == 1.0 and table["edit_plan"]["de"]["time_validity"] == 1.0
    assert table["hook_writer"]["en"]["platform_fit"] is None
    assert Q.below_pass_line(table) == []


async def test_a_careless_model_is_caught_by_the_scores_and_the_drop_check_blocks_it():
    base = await Q.score(good_model, languages=["en", "es"], limit=6)
    bad = await Q.score(careless_model, languages=["en", "es"], limit=6)
    assert bad["hook_writer"]["en"]["grounding"] < 1.0 and bad["script_from_text"]["es"]["grounding"] < 1.0
    assert bad["caption_polish"]["en"]["length_fit"] < 1.0
    blocked = Q.drops(bad, base)
    assert any("hook_writer / en / grounding" in b for b in blocked)
    assert Q.drops(base, base) == []
    assert Q.drops(bad, base, tolerance=1.0) == []
    weak = Q.below_pass_line(bad)
    assert any("hook_writer / en / grounding" in w for w in weak)


def test_a_score_that_is_no_longer_produced_counts_as_a_drop():
    base = {"hook_writer": {"en": {"grounding": 1.0, "language": None}}}
    assert Q.drops({"hook_writer": {"en": {"grounding": None}}}, base) == ["hook_writer / en / grounding: no longer scored (was 1.00)"]
    assert Q.drops({"hook_writer": {"en": {"grounding": 0.97}}}, base) == []


def test_the_script_scores_recorded_answers_and_blocks_a_drop_with_a_nonzero_exit(tmp_path):
    script = str(ROOT / "scripts" / "video_quality_regression.py")
    baseline = tmp_path / "base.json"
    run = lambda *a: subprocess.run([sys.executable, script, "--language", "en", "--prompt", "hook_writer", "--limit", "3", "--baseline", str(baseline), *a],  # noqa: E731
                                    capture_output=True, text=True, cwd=ROOT, timeout=240)
    no_baseline = run("--check")
    assert no_baseline.returncode == 2 and "no baseline" in no_baseline.stdout.lower()
    # nothing recorded: every answer counts as failed, which is written as the (poor) baseline and passes against itself
    wrote = run("--write-baseline")
    assert wrote.returncode == 0 and json.loads(baseline.read_text())["hook_writer"]["en"]["grounding"] == 0.0
    same = run("--check")
    assert same.returncode == 0 and "No score dropped" in same.stdout
    base = json.loads(baseline.read_text())
    base["hook_writer"]["en"]["grounding"] = 1.0
    baseline.write_text(json.dumps(base))
    dropped = run("--check")
    assert dropped.returncode == 1 and "hook_writer / en / grounding" in dropped.stdout


def test_each_metric_has_its_own_pass_line_and_a_miss_says_which_line_it_missed():
    table = {"hook_writer": {"en": {"grounding": 0.9, "language": 0.9, "length_fit": 0.8, "platform_fit": None, "time_validity": None}}}
    out = Q.below_pass_line(table)
    assert out == ["hook_writer / en / grounding: 0.90 (needs 0.95)", "hook_writer / en / length_fit: 0.80 (needs 0.85)"]
    assert Q.PASS_LINES["grounding"] > Q.PASS_LINES["length_fit"]
    assert Q.below_pass_line(table, lines={"grounding": 0.5, "length_fit": 0.5, "language": 0.5}) == []
