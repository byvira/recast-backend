"""The shared rule cases still give the answers the server's rules give.

tests/fixtures/prompt_library_rule_cases.json is also checked by the frontend unit tests against the screens' rules. If
this fails, the server's rules changed: re-run scripts/export_prompt_rule_cases.py (it writes both copies) and make the
screens' rules give the same answers.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_shared_rule_cases_match_the_server_rules():
    sys.path.insert(0, str(ROOT / "scripts"))
    import export_prompt_rule_cases as cases

    saved = json.loads((ROOT / "tests" / "fixtures" / "prompt_library_rule_cases.json").read_text(encoding="utf-8"))
    fresh = json.loads(cases.text())
    stale = [k for k in fresh if fresh[k] != saved.get(k)]
    assert not stale, f"the shared rule cases are out of date for: {stale}"
