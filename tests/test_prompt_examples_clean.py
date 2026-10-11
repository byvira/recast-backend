"""The examples inside the shared prompts follow the prompts' own rules: nothing invented, and nothing that belongs to one brand."""
import json
import re
from pathlib import Path

PROMPTS = Path(__file__).resolve().parent.parent / "app" / "prompts"
SHARED = [p for p in PROMPTS.rglob("*") if p.suffix in {".jinja", ".json", ".yaml"} and "localized" not in p.parts]

# Details of one brand, or a made-up result, that every other brand's output would otherwise inherit.
BRAND_LITERALS = ["Open Fold", "11 brands", "burned out", "last Tuesday", "4 money apps"]
# What claims.py treats as an unsupported claim: a percentage, "I ran ...", "studies show".
CLAIM_PATTERNS = [re.compile(r"\b\d{1,3}\s?%"), re.compile(r"\bstudies show\b", re.I), re.compile(r"\bresearch says\b", re.I)]


def test_no_shared_prompt_carries_one_brands_details():
    hits = [f"{p.relative_to(PROMPTS)}: {lit}" for p in SHARED for lit in BRAND_LITERALS if lit in p.read_text(encoding="utf-8")]
    assert hits == []


def test_the_good_examples_contain_no_invented_figures():
    for name in ("generator_good_bad_pairs.json", "generator_example_tweet.json", "scorer_examples.json"):
        data = json.loads((PROMPTS / "fixtures" / name).read_text(encoding="utf-8"))
        good = []
        if isinstance(data, list):
            good = [str(item.get("good", "")) for item in data]
        elif "text" in data:
            good = [str(data["text"])]
        else:
            good = [str(v.get("good", "")) for v in data.values()]
        for text in good:
            assert not any(pattern.search(text) for pattern in CLAIM_PATTERNS), f"{name}: {text}"
            assert not re.search(r"\b\d+\s+(weeks?|days?|brands?|apps?|clients?)\b", text), f"{name}: {text}"


def test_a_prompt_version_changes_when_the_prompt_text_changes():
    from app.prompts.registry import template_version, text_version

    assert template_version("support/draft_reply").startswith("support/draft_reply@")
    assert template_version("support/draft_reply") == template_version("support/draft_reply")
    assert text_version("x", "one wording") != text_version("x", "another wording")
    assert template_version("does/not/exist").endswith("@unknown")
