"""Writes the shared rule cases for the Ops prompt library: inputs and the answers the server's rules give.

The same file is checked by the backend tests (the answers must still be what app/prompt_library/rules.py gives) and by
the frontend unit tests (the screens' rules in lib/prompt-library must give the same answers), so the two copies of the
rules can never disagree without a test failing.

Run from the backend folder:  .venv/Scripts/python.exe scripts/export_prompt_rule_cases.py [frontend tests/unit/fixtures dir]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.prompt_library import regions as R  # noqa: E402
from app.prompt_library import rules  # noqa: E402

BACKEND_FIXTURE = ROOT / "tests" / "fixtures" / "prompt_library_rule_cases.json"

MARKED = (
    '{# @locked locked_1 #}\nYou write for {{ brand }}.\n{% if tone %}Tone: {{ tone }}.\n{% endif %}\n{# @end #}\n'
    '{# @editable craft "Craft" #}\nWrite like a person. Keep [BRAND NAME] as it is.\n{# @end #}\n'
    '{# @editable close "Closing" #}\nEnd with one question about {{ topic }}.\n{# @end #}\n'
)
NO_END = '{# @editable all "Prompt text" #}\nNo final newline here'
STRAY = "Before the markers.\n{# @locked a #}\nx\n{# @end #}\nAfter.\n"
UNCLOSED = '{# @locked a #}\nKept even with no end marker.\n{# @editable b "B" #}\nSecond.\n{# @end #}\n'

SLOT_TEXTS = [
    "Hello {{ name }} and {{ topic | upper }}.",
    "{% for item in items %}{{ item.title }}{% endfor %}",
    "{% if not ready %}wait{% elif mode %}{{ mode }}{% endif %}",
    "{% set n = 3 %}{{ n }} {{ (title) }}",
    "{{- spaced -}} {% if true %}x{% endif %}",
]


def _checks_cases() -> list[dict]:
    regions = R.parse_regions(MARKED)
    edits = [
        {"craft": "Write like a thoughtful person. Keep [BRAND NAME] as it is.\n"},
        {"craft": "Write plainly.\n"},
        {"close": "End with one question.\n"},
        {"close": "End with {{ topic }} and {{ extra }}.\n"},
        {"close": "End with {{ topic }} and {{ audience }}.\n"},
        {"craft": "{% if x %}Keep [BRAND NAME].{% endif %}\n"},
        {"craft": "   \n"},
        {"craft": "Keep [BRAND NAME]. " + "x" * 6001},
        {"craft": "Keep [BRAND NAME]. The previous attempt failed, so try again.\n"},
        {"locked_1": "Changed.\n"},
        {"not_a_section": "Anything.\n"},
        {"craft": "Keep [BRAND NAME]. {{ brand.__class__ }}\n"},
        {"craft": "Keep [BRAND NAME]. {{ audience }} please.\n"},
        {"craft": "Keep [BRAND NAME]. {#\n"},
        {"craft": "Keep [BRAND NAME]. {{ topic\n"},
    ]
    out = []
    for e in edits:
        out.append({"edits": e, "protected": ["[BRAND NAME]"], "extra": ["audience"],
                    "expected": rules.run_free_checks(regions, e, ["[BRAND NAME]"], ["audience"])})
    return out


def _gate_cases() -> list[dict]:
    passing = [{"id": "a", "name": "A", "required": True, "status": "pass", "detail": ""}]
    failing = passing + [{"id": "b", "name": "B", "required": True, "status": "fail", "detail": ""}]
    warning = passing + [{"id": "c", "name": "C", "required": False, "status": "warn", "detail": ""}]
    erroring = failing + [{"id": "d", "name": "D", "required": False, "status": "error", "detail": ""}]
    inputs = [(None, False), ([], False), (passing, True), (passing, False), (failing, False), (warning, False), (erroring, False)]
    return [{"results": r, "stale": s, "expected": rules.evaluate_gate(r, s)} for r, s in inputs]


def _ops_cases() -> list[dict]:
    rows = {
        "register_notes": [
            [{"code": "ta", "language": "Tamil", "note": "Everyday Tamil."}],
            [{"code": "hi", "language": "Hindi", "note": "x" * 401}],
            [{"code": "hi", "language": "Hindi", "note": "Use {{ name }}"}],
        ],
        "tone_options": [
            [{"id": "formal", "label": "Formal", "instruction": "Write formally with full sentences.", "enabled": True}],
            [{"id": "formal", "label": "", "instruction": "short", "enabled": False}],
            [{"id": "a", "label": "A", "instruction": "Write formally with full sentences.", "enabled": True},
             {"id": "a", "label": "B", "instruction": "Write formally with {% x %} sentences.", "enabled": True}],
            [{"id": "3_words", "label": "Three", "instruction": "Write formally with full sentences.", "enabled": True}],
            [],
        ],
        "content_goals": [
            [],
            [{"id": "educate", "text": "Teach one useful thing clearly."}],
            [{"id": "educate", "text": "Too short"}, {"id": "sell", "text": "y" * 601}],
            [{"id": "", "text": "Teach one useful thing clearly."}, {"id": "sell", "text": "Sell one thing clearly please."},
             {"id": "sell", "text": "Sell another thing clearly now."}],
        ],
        "quality_words": [
            [{"language": "en", "list": "generic_openings", "label": "Generic openings", "words": ["in today's world"]}],
            [{"language": "en", "list": "filler_words", "label": "Filler words", "words": []}],
            [{"language": "en", "list": "filler_words", "label": "Filler words", "words": ["many", "Many ", "z" * 81]}],
            [{"language": "en", "list": "filler_words", "label": "Filler words", "words": ["", "a", "ok"]}],
            [{"language": "en", "list": "filler_words", "label": "Filler words", "words": "abc"}],
        ],
    }
    return [{"kind": k, "rows": r, "expected": rules.validate_ops_data(k, r)} for k, many in rows.items() for r in many]


def _assertion_cases() -> list[dict]:
    rendered = "Write about habits for Asha. தமிழ் {{ missing }}"
    asserts = [
        {"kind": "includes", "text": "habits"}, {"kind": "includes", "text": "goals"},
        {"kind": "excludes", "text": "goals"}, {"kind": "excludes", "text": "Asha"},
        {"kind": "starts_with", "text": "Write"}, {"kind": "starts_with", "text": "Read"},
        {"kind": "min_chars", "value": 10}, {"kind": "min_chars", "value": 500},
        {"kind": "max_chars", "value": 500}, {"kind": "max_chars", "value": 10},
        {"kind": "max_tokens", "value": 100}, {"kind": "max_tokens", "value": 2},
        {"kind": "no_unfilled"},
    ]
    out = [{"assertion": a, "rendered": rendered, "expected": rules.evaluate_assertion(a, rendered)} for a in asserts]
    out.append({"assertion": {"kind": "no_unfilled"}, "rendered": "All filled.",
                "expected": rules.evaluate_assertion({"kind": "no_unfilled"}, "All filled.")})
    return out


def _case_validation() -> list[dict]:
    cases = [
        {"id": "case_1", "name": "Fills every variable", "values": {}, "assertions": [{"kind": "no_unfilled"}]},
        {"id": "case_2", "name": "x", "values": {}, "assertions": []},
        {"id": "case_3", "name": "Text checks", "values": {}, "assertions": [{"kind": "includes", "text": " "}, {"kind": "max_chars", "value": 0}]},
    ]
    return [{"case": c, "expected": rules.validate_case(c)} for c in cases]


def _new_prompt_cases() -> list[dict]:
    paths = ["text/generate/master"]
    folders = ["text", "text/generate"]
    inputs = [
        {"folder": "text/generate", "name": "new_one", "reason": "Used for a test", "text": "Write one specific opening line."},
        {"folder": "", "name": "", "reason": "", "text": ""},
        {"folder": "Text/Bad", "name": "1bad", "reason": "short", "text": "tiny"},
        {"folder": "text/missing", "name": "ok_name", "reason": "Used for a test", "text": "Write one specific opening line."},
        {"folder": "text/generate", "name": "master", "reason": "Used for a test", "text": "{# @locked x #}\nWrite one specific opening line."},
    ]
    return [{"input": i, "paths": paths, "folders": folders,
             "expected": rules.validate_new_prompt(i, set(paths), set(folders))} for i in inputs]


def _new_folder_cases() -> list[dict]:
    folders = ["text", "text/generate"]
    inputs = [{"parent": "text", "name": "lab"}, {"parent": "nope", "name": ""}, {"parent": "", "name": "Text"},
              {"parent": "text", "name": "generate"}]
    return [{"input": i, "folders": folders, "expected": rules.validate_new_folder(i, set(folders))} for i in inputs]


def build() -> dict:
    return {
        "slots": [{"text": t, "expected": R.find_slots(t)} for t in SLOT_TEXTS],
        "tags": [{"text": t, "expected": R.find_tags(t)} for t in SLOT_TEXTS],
        "regions": [{"text": t, "expected": [r.as_dict() for r in R.parse_regions(t)]} for t in (MARKED, NO_END, STRAY, UNCLOSED)],
        "compose": [{"text": MARKED, "overrides": {"craft": "New craft", "locked_1": "Never"},
                     "expected": R.compose(R.parse_regions(MARKED), {"craft": "New craft", "locked_1": "Never"})}],
        "checks": _checks_cases(),
        "gate": _gate_cases(),
        "opsData": _ops_cases(),
        "assertions": _assertion_cases(),
        "caseValidation": _case_validation(),
        "newPrompt": _new_prompt_cases(),
        "newFolder": _new_folder_cases(),
        "renderSample": [{"text": "Hi {{ first_name }}, {{ topic | title }} {% if x %}{{ unknown }}{% endif %}",
                          "values": {"first_name": "Asha", "topic": "habits"},
                          "expected": rules.render_sample("Hi {{ first_name }}, {{ topic | title }} {% if x %}{{ unknown }}{% endif %}",
                                                          {"first_name": "Asha", "topic": "habits"})}],
    }


def text() -> str:
    return json.dumps(build(), ensure_ascii=False, indent=1, sort_keys=True) + "\n"


if __name__ == "__main__":
    body = text()
    BACKEND_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    BACKEND_FIXTURE.write_bytes(body.encode("utf-8"))
    if len(sys.argv) > 1:
        out = Path(sys.argv[1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "prompt_library_rule_cases.json").write_bytes(body.encode("utf-8"))
    print("wrote the shared rule cases")
