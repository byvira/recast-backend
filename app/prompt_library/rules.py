"""The rules of the Ops prompt library, enforced on the server.

These match the rules the Ops screens show (`lib/prompt-library/logic.ts`, `suite.ts`, `ops-data.ts`, `collab.ts`); the
shared cases in tests/fixtures/prompt_library_rule_cases.json run against both so the two can never disagree. Everything
here is pure: state in, state out, no database. Data uses the same field names as the screens (camelCase), so what is
stored is what the screens read.
"""

from __future__ import annotations

import math
import re
from typing import Any

from app.prompt_library.regions import Region, compose, find_slots, find_tags

Result = tuple[bool, Any]  # (True, value) or (False, plain message)

MAX_REGION_CHARS = 6000
MIN_NOTE_CHARS = 10
MIN_OVERRIDE_CHARS = 20
MIN_NEW_TEXT_CHARS = 20
STALE_DRAFT_DAYS = 30
LEAK_WORDING = ["previous attempt failed", "rewrite feedback", "enforcement context for this retry"]
FORBIDDEN = "You don't have access to this. Ask an admin."


def ok(value: Any = None) -> Result:
    return True, value


def fail(message: str) -> Result:
    return False, message


# ---------------------------------------------------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------------------------------------------------

ROLES = ("editor", "publisher", "admin")
ALLOWED: dict[str, tuple[str, ...]] = {
    "edit_draft": ROLES,
    "run_checks": ROLES,
    "request_publish": ROLES,
    "edit_ops_data": ROLES,
    "propose_class": ROLES,
    "create_prompt": ROLES,
    "comment": ROLES,
    "publish": ("publisher", "admin"),
    "approve": ("publisher", "admin"),
    "rollback": ("publisher", "admin"),
    "override_gate": ("publisher", "admin"),
    "sign_off": ("publisher", "admin"),
    "manage_access": ("admin",),
    "manage_rules": ("admin",),
}


def can(role: str, action: str) -> bool:
    return role in ALLOWED.get(action, ())


# ---------------------------------------------------------------------------------------------------------------------
# Checking an edit (no model call)
# ---------------------------------------------------------------------------------------------------------------------

OUTPUT = re.compile(r"\{\{-?([\s\S]*?)-?\}\}")
PLAIN_OUTPUT = re.compile(r"^\s*[a-zA-Z_]\w*\s*$")


def find_outputs(text: str) -> list[str]:
    """What is inside each `{{ ... }}` of a text."""
    return [m.group(1) for m in OUTPUT.finditer(text)]


def _norm(expression: str) -> str:
    return re.sub(r"\s+", "", expression)


def check_region_edit(original: Region, edited: str, protected: list[str], extra_slots: list[str]) -> list[dict]:
    issues: list[dict] = []
    rid = original.id

    def add(code: str, message: str) -> None:
        issues.append({"code": code, "regionId": rid, "message": message})

    if original.kind == "locked":
        if edited != original.body:
            add("locked_region", "That part is locked. It's set in code.")
        return issues
    if not edited.strip():
        add("empty", "This section can't be empty.")
    if len(edited) > MAX_REGION_CHARS:
        add("too_long", f"Keep this section under {MAX_REGION_CHARS} characters.")
    before = set(find_slots(original.body))
    after = set(find_slots(edited))
    allowed = before | set(extra_slots)
    for name in sorted(after):
        if name not in allowed:
            add("unknown_slot", f"{{{name}}} isn't a variable for this prompt.")
    for name in sorted(before):
        if name not in after:
            add("missing_slot", f"Add {{{name}}} back. The prompt needs it.")
    if "\n".join(find_tags(original.body)) != "\n".join(find_tags(edited)):
        add("logic_changed", "The template logic in this section changed. Put the tags back as they were.")
    # Only plain variables like {{ name }} can be added. Anything more in the curly brackets is code, and code is set in the
    # files, so an expression the section did not already have is refused.
    kept = {_norm(e) for e in find_outputs(original.body)}
    if any(_norm(e) not in kept and not PLAIN_OUTPUT.match(e) for e in find_outputs(edited)):
        add("logic_changed", "Only plain variables like {{ name }} can be added. Put the rest back as it was.")
    # A comment opened or closed here would hide text in the sections around it, and unmatched brackets break the prompt.
    if ("{#" in edited or "#}" in edited) and edited.count("{#") != original.body.count("{#"):
        add("logic_changed", "Remove {# and #}. They would hide text in the rest of the prompt.")
    if edited.count("{{") != edited.count("}}") or edited.count("{%") != edited.count("%}"):
        add("logic_changed", "A curly bracket is not closed. Every {{ needs a }}.")
    for literal in protected:
        if literal in original.body and literal not in edited:
            add("literal_removed", f"Keep {literal}. Members replace it." if literal.startswith("[")
                else "Keep the protected text in this section. The code depends on it.")
    low = edited.lower()
    for phrase in LEAK_WORDING:
        if phrase in low and phrase not in original.body.lower():
            add("leak_wording", "That wording is internal and can leak into answers. Reword it.")
    return issues


def run_free_checks(regions: list[Region], edits: dict[str, str], protected: list[str], extra_slots: list[str]) -> list[dict]:
    by_id = {r.id: r for r in regions}
    results: list[dict] = []
    # An edit to a locked section, or to one the file doesn't have, is refused.
    touched = [rid for rid in edits if rid not in by_id or by_id[rid].kind != "editable"]
    results.append({
        "id": "locked_integrity", "name": "Locked parts are unchanged", "required": True,
        "status": "pass" if not touched else "fail",
        "detail": "Every locked section is exactly as it is in code." if not touched else f"Locked: {', '.join(touched)}.",
    })
    issues: list[dict] = []
    for r in regions:
        if r.kind == "editable" and r.id in edits:
            issues.extend(check_region_edit(r, edits[r.id], protected, extra_slots))

    def pick(codes: tuple[str, ...]) -> list[dict]:
        return [i for i in issues if i["code"] in codes]

    def result(rid: str, name: str, found: list[dict], passed: str, required: bool = True, bad: str = "fail") -> dict:
        return {"id": rid, "name": name, "required": required, "status": "pass" if not found else bad,
                "detail": passed if not found else " ".join(i["message"] for i in found)}

    results.append(result("variables", "Variables resolve", pick(("unknown_slot", "missing_slot")),
                          "No unknown or missing variables."))
    results.append(result("template_logic", "Template logic is unchanged", pick(("logic_changed",)),
                          "All template tags match the code."))
    if protected:
        results.append(result("protected_text", "Protected text is present", pick(("literal_removed",)),
                              f"{', '.join(protected)} still present." if all(p.startswith("[") for p in protected)
                              else "Every protected part is still present."))
    results.append(result("size", "Sections are a sensible size", pick(("empty", "too_long")),
                          "No section is empty or too long."))
    results.append(result("no_leak_wording", "No internal wording", pick(("leak_wording",)),
                          "Nothing that could leak into an answer.", required=False, bad="warn"))
    return results


def evaluate_gate(results: list[dict] | None, stale: bool) -> dict:
    if not results:
        return {"status": "none", "failing": [], "warnings": []}
    if stale:
        return {"status": "stale", "failing": [], "warnings": []}
    failing = [r["name"] for r in results if r["required"] and r["status"] == "fail"]
    errors = [r["name"] for r in results if r["status"] == "error"]
    warnings = [r["name"] for r in results if r["status"] == "warn"]
    if errors:
        return {"status": "error", "failing": errors, "warnings": warnings}
    if failing:
        return {"status": "failed", "failing": failing, "warnings": warnings}
    return {"status": "passed", "failing": [], "warnings": warnings}


# ---------------------------------------------------------------------------------------------------------------------
# The workflow: drafts, publishing, rollback, code changes (state in, state out)
# ---------------------------------------------------------------------------------------------------------------------

def empty_state(regions: list[Region], repo_hash: str) -> dict:
    return {
        "versions": [],
        "draft": None,
        "defaults": {r.id: r.body for r in regions if r.kind == "editable"},
        "repoHash": repo_hash,
        "repoChange": None,
        "orphaned": [],
        "checks": None,
    }


def live_version(state: dict) -> dict | None:
    return next((v for v in state["versions"] if v["status"] == "live"), None)


def effective_texts(state: dict) -> dict[str, str]:
    """Every editable section's text right now: the live version's where there is one, else the code default."""
    out = dict(state["defaults"])
    live = live_version(state)
    if live:
        for rid, text in live["regionTexts"].items():
            if rid in state["defaults"] and rid not in state["orphaned"]:
                out[rid] = text
    return out


def save_draft(state: dict, edits: dict[str, str], expected_revision: int, who: str, now: str) -> Result:
    current = (state["draft"] or {}).get("revision", 0) if state["draft"] else 0
    if expected_revision != current:
        return fail("Someone else changed this draft. Reload to see their changes.")
    # Revisions only go up, even across publishes and discards, so an old screen can never match a newer draft.
    revision = max(current, int(state.get("draftSeq") or 0)) + 1
    live = live_version(state)
    draft = {"edits": edits, "revision": revision, "baseVersionId": live["id"] if live else None,
             "savedBy": who, "savedAt": now, "checksStale": True}
    return ok({**state, "draft": draft, "draftSeq": revision})


def discard_draft(state: dict) -> dict:
    return {**state, "draft": None, "checks": None}


def record_checks(state: dict, results: list[dict]) -> dict:
    draft = {**state["draft"], "checksStale": False} if state["draft"] else None
    return {**state, "checks": results, "draft": draft}


def _new_version(state: dict, texts: dict, note: str, who: str, now: str, **extra: Any) -> dict:
    number = (state["versions"][0]["number"] if state["versions"] else 0) + 1
    version = {
        "id": f"v{number}", "number": number, "regionTexts": texts, "changeNote": note, "publishedBy": who,
        "approvedBy": None, "publishedAt": now, "repoHashAtPublish": state["repoHash"], "gateResult": "passed",
        "overrideReason": None, "status": "live", "restoredFrom": None,
    }
    version.update(extra)
    return version


def _with_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def _only_changed(texts: dict[str, str], defaults: dict[str, str]) -> dict[str, str]:
    """A version keeps only the sections whose text differs from the code, so a later code fix to any other section
    reaches the product."""
    return {rid: t for rid, t in texts.items() if rid not in defaults or _with_newline(t) != _with_newline(defaults[rid])}


def _make_live(state: dict, version: dict, old_status: str = "superseded") -> list[dict]:
    return [version] + [{**v, "status": old_status} if v["status"] == "live" else v for v in state["versions"]]


def publish(state: dict, *, who: str, now: str, change_note: str, approved_by: str | None, override_reason: str | None,
            require_approval: bool, allow_override: bool, approver_is_requester: bool = False) -> Result:
    if not state["draft"]:
        return fail("There are no unpublished changes.")
    if len(change_note.strip()) < MIN_NOTE_CHARS:
        return fail("Add a change note.")
    gate = evaluate_gate(state["checks"], state["draft"]["checksStale"])
    gate_result = "passed"
    if gate["status"] in ("stale", "none"):
        return fail("Tests are out of date. Run them again.")
    if gate["status"] == "error":
        return fail("Couldn't run every test. Run them again.")
    if gate["status"] == "failed":
        if not allow_override:
            n = len(gate["failing"])
            return fail(f"{n} required test{' is' if n == 1 else 's are'} failing.")
        if len((override_reason or "").strip()) < MIN_OVERRIDE_CHARS:
            return fail("Give a reason of at least 20 characters to publish with failing tests.")
        gate_result = "overridden"
    if require_approval:
        if not approved_by:
            return fail("Waiting for approval.")
        if approver_is_requester:
            return fail("A second person has to approve this.")
    live = live_version(state)
    texts = _only_changed({**(live["regionTexts"] if live else {}), **state["draft"]["edits"]}, state["defaults"])
    version = _new_version(state, texts, change_note.strip(), who, now, approvedBy=approved_by,
                           gateResult=gate_result,
                           overrideReason=(override_reason or "").strip() if gate_result == "overridden" else None)
    return ok({**state, "versions": _make_live(state, version), "draft": None, "checks": None, "repoChange": None})


def rollback(state: dict, to_number: int, who: str, now: str, reason: str) -> Result:
    if len(reason.strip()) < MIN_NOTE_CHARS:
        return fail("Add a reason.")
    target = next((v for v in state["versions"] if v["number"] == to_number), None)
    if not target:
        return fail("That version doesn't exist.")
    if target["gateResult"] == "failed":
        return fail("That version never passed its tests.")
    version = _new_version(state, dict(target["regionTexts"]), f"Rolled back to v{to_number}. {reason.strip()}", who, now,
                           gateResult=target["gateResult"], overrideReason=target.get("overrideReason"),
                           restoredFrom=to_number)
    return ok({**state, "versions": _make_live(state, version, "rolled_back"), "draft": None, "checks": None})


def revert_to_default(state: dict, region_ids: list[str], who: str, now: str, reason: str) -> Result:
    if len(reason.strip()) < MIN_NOTE_CHARS:
        return fail("Add a reason.")
    live = live_version(state)
    if not live:
        return fail("There is no published text to remove.")
    unknown = [rid for rid in region_ids if rid not in state["defaults"]]
    if unknown:
        return fail("That section no longer exists. Reload and try again.")
    # The section goes back to whatever the code says, now and after later code changes.
    texts = {rid: t for rid, t in live["regionTexts"].items() if rid not in region_ids}
    version = _new_version(state, texts, f"Back to the code default for {', '.join(region_ids)}. {reason.strip()}", who, now,
                           gateResult=live["gateResult"], overrideReason=live.get("overrideReason"))
    return ok({**state, "versions": _make_live(state, version), "repoChange": None})


def apply_repo_deploy(state: dict, new_regions: list[Region], repo_hash: str, now: str) -> dict:
    """What a deploy did to a prompt: new code defaults, a changed default under a live version, removed sections."""
    new_defaults = {r.id: r.body for r in new_regions if r.kind == "editable"}
    live = live_version(state)
    orphaned = [rid for rid in live["regionTexts"] if rid not in new_defaults] if live else []
    repo_change = state.get("repoChange")
    if live and repo_change is None:
        # Only a section the live version has its own text for needs a decision; every other section simply follows the code.
        for rid, text in new_defaults.items():
            old = state["defaults"].get(rid)
            if rid in live["regionTexts"] and old is not None and old != text:
                repo_change = {"regionId": rid, "oldDefault": old, "newDefault": text, "at": now}
                break
    return {**state, "defaults": new_defaults, "repoHash": repo_hash, "repoChange": repo_change, "orphaned": orphaned}


def clear_orphans(state: dict, who: str, now: str) -> dict:
    """Drops the text for sections the code no longer has, as a new version, so the history shows what went live."""
    if not state["orphaned"]:
        return state
    live = live_version(state)
    if not live:
        return {**state, "orphaned": []}
    texts = {rid: t for rid, t in live["regionTexts"].items() if rid not in state["orphaned"]}
    version = _new_version(state, texts, f"Cleared text for sections removed in code: {', '.join(state['orphaned'])}.",
                           who, now, gateResult=live["gateResult"], overrideReason=live.get("overrideReason"))
    return {**state, "orphaned": [], "versions": _make_live(state, version)}


def resolve_repo_change(state: dict, choice: str, who: str, now: str) -> dict:
    """Keep the live text (clears the notice) or take the new code default for that section (a new version)."""
    change = state.get("repoChange")
    if not change:
        return state
    if choice == "keep_override":
        return {**state, "repoChange": None}
    live = live_version(state)
    if not live:
        return {**state, "repoChange": None}
    rid = change["regionId"]
    texts = {k: t for k, t in live["regionTexts"].items() if k != rid}
    version = _new_version(state, texts, f"Took the new code default for {rid}.", who, now,
                           gateResult=live["gateResult"], overrideReason=live.get("overrideReason"))
    return {**state, "repoChange": None, "versions": _make_live(state, version)}


def attention_flags(state: dict, signed_off: bool, now_ms: float) -> list[str]:
    from datetime import datetime

    flags: list[str] = []
    draft = state["draft"]
    gate = evaluate_gate(state["checks"], bool(draft and draft["checksStale"]))
    if gate["status"] == "failed":
        flags.append("failing_tests")
    if state["orphaned"]:
        flags.append("orphaned_override")
    if state.get("repoChange"):
        flags.append("repo_default_changed")
    if draft:
        saved = datetime.fromisoformat(draft["savedAt"].replace("Z", "+00:00")).timestamp() * 1000
        if now_ms - saved > STALE_DRAFT_DAYS * 86_400_000:
            flags.append("stale_draft")
    if not signed_off:
        flags.append("unsigned")
    return flags


def override_status(state: dict) -> str:
    if state["draft"]:
        return "draft"
    return "live_override" if live_version(state) else "repo_default"


def tests_health(state: dict) -> str:
    if not state["draft"] and not state["checks"]:
        return "none"
    gate = evaluate_gate(state["checks"], bool(state["draft"] and state["draft"]["checksStale"]))
    return {"failed": "failing", "error": "failing", "stale": "stale", "passed": "passing"}.get(gate["status"], "none")


# ---------------------------------------------------------------------------------------------------------------------
# New prompts and folders
# ---------------------------------------------------------------------------------------------------------------------

PATH_SEGMENT = re.compile(r"^[a-z][a-z0-9_]*$")
MARKER_TEXT = re.compile(r"^\{#\s*@(locked|editable|end)", re.M)


def _trim_slashes(text: str) -> str:
    return text.strip().strip("/")


def validate_new_prompt(data: dict, existing_paths: set[str], existing_folders: set[str]) -> list[dict]:
    issues: list[dict] = []
    folder = _trim_slashes(data.get("folder", ""))
    name = data.get("name", "").strip()
    if not folder:
        issues.append({"field": "folder", "message": "Choose a folder, or type a new one."})
    elif not all(PATH_SEGMENT.match(p) for p in folder.split("/")):
        issues.append({"field": "folder", "message": "Folder names use lower case letters, numbers and underscores, and start with a letter."})
    elif folder not in existing_folders:
        issues.append({"field": "folder", "message": "That folder does not exist. Create the folder first, then add the prompt."})
    if not name:
        issues.append({"field": "name", "message": "Give the prompt a name."})
    elif not PATH_SEGMENT.match(name):
        issues.append({"field": "name", "message": "Use lower case letters, numbers and underscores, starting with a letter."})
    if folder and name and f"{folder}/{name}" in existing_paths:
        issues.append({"field": "name", "message": "A prompt with that name already exists in this folder."})
    if len(data.get("reason", "").strip()) < MIN_NOTE_CHARS:
        issues.append({"field": "reason", "message": f"Say where it will be used (at least {MIN_NOTE_CHARS} characters)."})
    text = data.get("text", "")
    if len(text.strip()) < MIN_NEW_TEXT_CHARS:
        issues.append({"field": "text", "message": f"Write the prompt (at least {MIN_NEW_TEXT_CHARS} characters)."})
    elif MARKER_TEXT.search(text):
        issues.append({"field": "text", "message": "Remove the region marker lines. They are added for you."})
    return issues


def wrap_new_prompt(text: str) -> str:
    body = text if text.endswith("\n") else text + "\n"
    return '{# @editable all "Prompt text" #}\n' + body + "{# @end #}\n"


def validate_new_folder(data: dict, existing_folders: set[str]) -> list[dict]:
    issues: list[dict] = []
    parent = _trim_slashes(data.get("parent", ""))
    name = data.get("name", "").strip()
    if parent and parent not in existing_folders:
        issues.append({"field": "parent", "message": "That parent folder does not exist."})
    if not name:
        issues.append({"field": "name", "message": "Give the folder a name."})
    elif not PATH_SEGMENT.match(name):
        issues.append({"field": "name", "message": "Use lower case letters, numbers and underscores, starting with a letter."})
    elif (f"{parent}/{name}" if parent else name) in existing_folders:
        issues.append({"field": "name", "message": "A folder with that name already exists here."})
    return issues


def new_folder_path(data: dict) -> str:
    parent = _trim_slashes(data.get("parent", ""))
    name = data.get("name", "").strip()
    return f"{parent}/{name}" if parent else name


# ---------------------------------------------------------------------------------------------------------------------
# The test suite (runs on the finished prompt text, no model call)
# ---------------------------------------------------------------------------------------------------------------------

TEXT_KINDS = ("includes", "excludes", "starts_with")
NUMBER_KINDS = ("min_chars", "max_chars", "max_tokens")
ASSERTION_KINDS = TEXT_KINDS + NUMBER_KINDS + ("no_unfilled",)
ASSERTION_LABEL = {
    "includes": "Contains the text",
    "excludes": "Does not contain the text",
    "starts_with": "Starts with the text",
    "min_chars": "Has at least this many characters",
    "max_chars": "Has at most this many characters",
    "max_tokens": "Is at most this many tokens",
    "no_unfilled": "Has no unfilled variable left",
}

SAMPLE_VALUES = {
    "language": "English",
    "language_name": "English",
    "language_instruction": "LANGUAGE: Respond entirely in English.",
    "language_short": "Write the whole output in English.",
    "language_line": "Write in English.",
    "platform": "LinkedIn",
    "platform_label": "LinkedIn",
    "content": "A few sentences of the member's own writing would appear here.",
    "brand_context": "Brand voice: warm, direct, plain words.",
    "topic": "Why small habits beat big goals",
    "question": "Which posts worked best last week?",
    "instruction": "Make the opening sharper.",
    "banned_words": "synergy, leverage",
    "first_name": "Asha",
    "subject": "I can't connect Instagram",
}

UNFILLED = re.compile(r"\{\{-?\s*\(?\s*([a-zA-Z_]\w*)[^}]*?-?\}\}")


def sample_values(names: list[str]) -> dict[str, str]:
    return {n: SAMPLE_VALUES.get(n, f"[{n}]") for n in names}


def render_sample(text: str, values: dict[str, str]) -> str:
    """Fills `{{ name ... }}` with values. Template tags stay visible, so the real structure is not hidden."""
    return UNFILLED.sub(lambda m: values[m.group(1)] if m.group(1) in values else m.group(0), text)


def _tokens(text: str) -> int:
    latin = other = 0
    for ch in text:
        if ch.isspace():
            continue
        if ord(ch) > 0x024F:
            other += 1
        else:
            latin += 1
    return math.ceil(latin / 4 + other / 1.5)


def _js_length(text: str) -> int:
    """Length as the screens count it (UTF-16 code units)."""
    return len(text.encode("utf-16-le")) // 2


def evaluate_assertion(a: dict, rendered: str) -> dict:
    kind = a.get("kind")
    text = a.get("text") or ""
    value = a.get("value") or 0

    def res(passed: bool, message: str) -> dict:
        return {"assertion": a, "ok": passed, "message": message}

    if kind == "includes":
        return res(True, "Found it.") if text in rendered else res(False, f'The text "{text[:60]}" is missing.')
    if kind == "excludes":
        return res(False, f'The text "{text[:60]}" is in the prompt.') if text in rendered else res(True, "Not present.")
    if kind == "starts_with":
        return res(True, "It starts that way.") if rendered.lstrip().startswith(text) else res(False, f"It doesn't start with \"{text[:60]}\".")
    length = _js_length(rendered)
    if kind == "min_chars":
        return res(True, f"{length} characters.") if length >= value else res(False, f"Only {length} characters. Needs at least {value}.")
    if kind == "max_chars":
        return res(True, f"{length} characters.") if length <= value else res(False, f"{length} characters. The limit is {value}.")
    if kind == "max_tokens":
        t = _tokens(rendered)
        return res(True, f"About {t} tokens.") if t <= value else res(False, f"About {t} tokens. The limit is {value}.")
    if kind == "no_unfilled":
        left = list(dict.fromkeys(m.group(1) for m in UNFILLED.finditer(rendered)))
        return res(True, "Every variable is filled.") if not left else res(False, f"Not filled: {', '.join(left)}.")
    return res(False, "That check isn't known.")


def run_case(case: dict, rendered: str) -> dict:
    results = [evaluate_assertion(a, rendered) for a in case.get("assertions") or []]
    passed = bool(results) and all(r["ok"] for r in results)
    return {"caseId": case["id"], "name": case["name"], "status": "pass" if passed else "fail", "results": results}


def summarise(results: list[dict]) -> dict:
    passed = sum(1 for r in results if r["status"] == "pass")
    return {"passed": passed, "failed": len(results) - passed, "total": len(results)}


def validate_case(case: dict) -> list[str]:
    issues: list[str] = []
    name = str(case.get("name") or "").strip()
    if len(name) < 3:
        issues.append("Give the test a name.")
    assertions = case.get("assertions") or []
    if not assertions:
        issues.append(f"{name or 'This test'} needs at least one check.")
    for a in assertions:
        kind = a.get("kind")
        if kind not in ASSERTION_KINDS:
            issues.append(f"{name or 'A test'}: that check isn't known.")
        elif kind in TEXT_KINDS and not str(a.get("text") or "").strip():
            issues.append(f'{name or "A test"}: "{ASSERTION_LABEL[kind]}" needs some text.')
        elif kind in NUMBER_KINDS and not (isinstance(a.get("value"), (int, float)) and a["value"] > 0):
            issues.append(f'{name or "A test"}: "{ASSERTION_LABEL[kind]}" needs a number above zero.')
    return issues


def suggested_cases(values: dict[str, str], protected: list[str]) -> list[dict]:
    cases = [
        {"id": "case_1", "name": "Fills every variable", "values": values, "assertions": [{"kind": "no_unfilled"}]},
        {"id": "case_2", "name": "Stays a sensible size", "values": values,
         "assertions": [{"kind": "min_chars", "value": 40}, {"kind": "max_tokens", "value": 3000}]},
    ]
    if protected:
        cases.append({"id": "case_3", "name": "Keeps the protected text", "values": values,
                      "assertions": [{"kind": "includes", "text": t} for t in protected]})
    return cases


def run_suite(regions: list[Region], texts: dict[str, str], cases: list[dict], against: str, now: str) -> dict:
    composed = compose(regions, texts)
    base = sample_values(find_slots(composed))
    results = [run_case(c, render_sample(composed, {**base, **(c.get("values") or {})})) for c in cases]
    return {"at": now, "against": against, "results": results}


# ---------------------------------------------------------------------------------------------------------------------
# Ops data lists
# ---------------------------------------------------------------------------------------------------------------------

MAX_NOTE_CHARS = 400
MIN_INSTRUCTION_CHARS = 20
MAX_INSTRUCTION_CHARS = 600
MAX_WORD_CHARS = 80
MIN_DATA_NOTE_CHARS = 10
TEMPLATE_SYNTAX = re.compile(r"\{\{|\}\}|\{%|%\}")
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
MIN_WORD_CHARS = 2
MAX_WORDS = 500


def validate_ops_data(kind: str, rows: Any) -> list[dict]:
    issues: list[dict] = []

    def add(at: str, message: str) -> None:
        issues.append({"at": at, "message": message})

    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return [{"at": "", "message": "That list isn't in the right shape."}]
    if kind == "register_notes":
        for r in rows:
            note = str(r.get("note") or "")
            if len(note) > MAX_NOTE_CHARS:
                add(r.get("code", ""), f"{r.get('language', '')}: keep the note under {MAX_NOTE_CHARS} characters.")
            if TEMPLATE_SYNTAX.search(note):
                add(r.get("code", ""), f"{r.get('language', '')}: remove the curly brackets. Notes are plain text.")
    elif kind == "tone_options":
        seen: set[str] = set()
        for r in rows:
            label = str(r.get("label") or "").strip()
            name = label or "A tone"
            instruction = str(r.get("instruction") or "")
            rid = str(r.get("id") or "")
            if not label:
                add(rid, "Every tone needs a name.")
            if rid in seen:
                add(rid, f"{name}: two tones share the same id.")
            seen.add(rid)
            if not re.match(r"^[a-z][a-z0-9_]*$", rid):
                add(rid, f"{name}: the id uses lower case letters, numbers and underscores.")
            if len(instruction.strip()) < MIN_INSTRUCTION_CHARS:
                add(rid, f"{name}: the instruction needs at least {MIN_INSTRUCTION_CHARS} characters.")
            if len(instruction) > MAX_INSTRUCTION_CHARS:
                add(rid, f"{name}: keep the instruction under {MAX_INSTRUCTION_CHARS} characters.")
            if TEMPLATE_SYNTAX.search(instruction):
                add(rid, f"{name}: remove the curly brackets. Instructions are plain text.")
        if not rows:
            add("", "Keep at least one tone.")
        elif not any(r.get("enabled") for r in rows):
            add("", "Leave at least one tone switched on.")
    elif kind == "content_goals":
        goal_ids: set[str] = set()
        for r in rows:
            gid = str(r.get("id") or "")
            if not ID_PATTERN.match(gid):
                add(gid, "Every goal needs an id of lower case letters, numbers and underscores.")
            elif gid in goal_ids:
                add(gid, f"{gid}: two goals share the same id.")
            goal_ids.add(gid)
            text = str(r.get("text") or "")
            preview = text.strip()[:28] or "A goal"
            if len(text.strip()) < MIN_INSTRUCTION_CHARS:
                add(str(r.get("id") or ""), f"{preview}: the goal needs at least {MIN_INSTRUCTION_CHARS} characters.")
            if len(text) > MAX_INSTRUCTION_CHARS:
                add(str(r.get("id") or ""), f"{preview}: keep the goal under {MAX_INSTRUCTION_CHARS} characters.")
            if TEMPLATE_SYNTAX.search(text):
                add(str(r.get("id") or ""), f"{preview}: remove the curly brackets.")
        if not rows:
            add("", "Keep at least one content goal.")
    elif kind == "quality_words":
        for r in rows:
            raw = r.get("words")
            key = f"{r.get('language')}:{r.get('list')}"
            label = f"{r.get('label')} ({r.get('language')})"
            if not isinstance(raw, list) or not all(isinstance(w, str) for w in raw):
                add(key, f"{label}: the list isn't in the right shape.")
                continue
            words = raw
            if not words:
                add(key, f"{label}: the list can't be empty.")
            if len(words) > MAX_WORDS:
                add(key, f"{label}: keep the list under {MAX_WORDS} entries.")
            # An empty or one-letter entry would match almost every post and fail it.
            short = next((w for w in words if len(w.strip()) < MIN_WORD_CHARS), None)
            if short is not None:
                add(key, f'{label}: "{short}" is too short. Each entry needs at least {MIN_WORD_CHARS} letters.')
            unclean = next((w for w in words if w != w.strip().lower()), None)
            if unclean is not None:
                add(key, f'{label}: write "{unclean.strip()[:24]}" in lower case with no spaces around it.')
            long = next((w for w in words if len(w) > MAX_WORD_CHARS), None)
            if long:
                add(key, f'{label}: "{long[:24]}..." is too long for one entry.')
            dupes = len(words) - len({w.strip().lower() for w in words})
            if dupes > 0:
                add(key, f"{label}: {dupes} entr{'y is' if dupes == 1 else 'ies are'} repeated.")
    else:
        add("", "That list doesn't exist.")
    return issues


def live_data(doc: dict) -> Any:
    live = next((v for v in doc["versions"] if v["status"] == "live"), None)
    return live["data"] if live else doc["codeDefault"]


def save_ops_draft(doc: dict, data: Any, who: str, now: str) -> dict:
    if data == live_data(doc):
        return {**doc, "draft": None}
    return {**doc, "draft": {"data": data, "savedBy": who, "savedAt": now}}


def _ops_version(doc: dict, data: Any, note: str, who: str, now: str, restored_from: int | None = None) -> dict:
    number = (doc["versions"][0]["number"] if doc["versions"] else 0) + 1
    return {"number": number, "data": data, "note": note, "by": who, "at": now, "status": "live", "restoredFrom": restored_from}


def _ops_make_live(doc: dict, version: dict, old_status: str = "superseded") -> list[dict]:
    return [version] + [{**v, "status": old_status} if v["status"] == "live" else v for v in doc["versions"]]


def publish_ops(kind: str, doc: dict, note: str, who: str, now: str) -> Result:
    if not doc.get("draft"):
        return fail("There are no unpublished changes.")
    if len(note.strip()) < MIN_DATA_NOTE_CHARS:
        return fail("Add a change note.")
    issues = validate_ops_data(kind, doc["draft"]["data"])
    if issues:
        return fail(f"{len(issues)} {'problem needs' if len(issues) == 1 else 'problems need'} fixing first.")
    version = _ops_version(doc, doc["draft"]["data"], note.strip(), who, now)
    return ok({**doc, "draft": None, "versions": _ops_make_live(doc, version)})


def rollback_ops(doc: dict, to_number: int, reason: str, who: str, now: str) -> Result:
    if len(reason.strip()) < MIN_DATA_NOTE_CHARS:
        return fail("Add a reason.")
    target = next((v for v in doc["versions"] if v["number"] == to_number), None)
    if not target:
        return fail("That version doesn't exist.")
    version = _ops_version(doc, target["data"], f"Rolled back to v{to_number}. {reason.strip()}", who, now, to_number)
    return ok({**doc, "draft": None, "versions": _ops_make_live(doc, version, "rolled_back")})


def revert_ops_to_code(doc: dict, reason: str, who: str, now: str) -> Result:
    if len(reason.strip()) < MIN_DATA_NOTE_CHARS:
        return fail("Add a reason.")
    if live_data(doc) == doc["codeDefault"]:
        return fail("This already matches the code.")
    version = _ops_version(doc, doc["codeDefault"], f"Back to the code values. {reason.strip()}", who, now)
    return ok({**doc, "draft": None, "versions": _ops_make_live(doc, version)})


# ---------------------------------------------------------------------------------------------------------------------
# People: access list, comments, notifications
# ---------------------------------------------------------------------------------------------------------------------

EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_COMMENT_CHARS = 1000


def admin_count(staff: list[dict]) -> int:
    return sum(1 for s in staff if s["role"] == "admin")


def check_add_staff(staff: list[dict], email: str, role: str) -> str | None:
    mail = email.strip().lower()
    if not EMAIL.match(mail):
        return "Enter a valid email address."
    if role not in ROLES:
        return "Choose a role."
    if any(s["email"].lower() == mail for s in staff):
        return "That person already has access."
    return None


def check_set_role(staff: list[dict], member_id: str, role: str) -> str | None:
    target = next((s for s in staff if s["id"] == member_id), None)
    if not target:
        return "That person isn't on the list."
    if role not in ROLES:
        return "Choose a role."
    if target["role"] == "admin" and role != "admin" and admin_count(staff) == 1:
        return "There has to be at least one admin."
    return None


def check_remove_staff(staff: list[dict], member_id: str) -> str | None:
    target = next((s for s in staff if s["id"] == member_id), None)
    if not target:
        return "That person isn't on the list."
    if target["role"] == "admin" and admin_count(staff) == 1:
        return "There has to be at least one admin."
    return None


def check_comment(text: str) -> str | None:
    if not text.strip():
        return "Write a comment first."
    if len(text) > MAX_COMMENT_CHARS:
        return f"Keep comments under {MAX_COMMENT_CHARS} characters."
    return None


def build_notifications(*, me: str, role: str, prompts: list[dict], requests: list[dict], comments: list[dict],
                        dismissed: set[str]) -> list[dict]:
    """In-app notices worked out from the current state. `me` is the person's id; names are shown."""
    out: list[dict] = []
    can_act = role != "editor"

    def href(path: str, tab: str = "") -> str:
        return f"/ops/prompts/file/{path}" + (f"?tab={tab}" if tab else "")

    for r in requests:
        if not r.get("approvedBy") and r.get("byId") != me and can_act:
            out.append({"id": f"req:{r['path']}:{r['at']}", "kind": "approval_needed", "title": f"{r['by']} asked to publish {r['path']}",
                        "detail": r.get("note", ""), "href": href(r["path"]), "at": r["at"]})
        if r.get("approvedBy") and r.get("byId") == me:
            out.append({"id": f"app:{r['path']}:{r.get('approvedAt')}", "kind": "request_approved",
                        "title": f"{r['approvedBy']} approved your publish of {r['path']}", "detail": "Open the prompt and publish it.",
                        "href": href(r["path"]), "at": r.get("approvedAt") or r["at"]})
    for p in prompts:
        at = p.get("changedAt") or "1970-01-01T00:00:00.000Z"
        flags = p.get("flags") or []
        path = p["path"]
        if "repo_default_changed" in flags and can_act:
            out.append({"id": f"code:{path}:{p.get('codeAt') or at}", "kind": "code_changed", "title": f"The code default changed for {path}",
                        "detail": "Decide whether to keep your published text.", "href": href(path), "at": at})
        if "orphaned_override" in flags and can_act:
            out.append({"id": f"orph:{path}:{p.get('orphans') or ''}", "kind": "orphaned", "title": f"{path} has text for a removed section",
                        "detail": "It is ignored. Clear it to tidy up.", "href": href(path), "at": at})
        if "failing_tests" in flags:
            out.append({"id": f"fail:{path}:{at}", "kind": "failing", "title": f"A required check is failing on {path}",
                        "detail": "Fix the draft or discard it.", "href": href(path, "tests"), "at": at})
    for item in comments:
        c = item["comment"]
        if not c.get("resolvedAt") and c.get("byId") != me:
            out.append({"id": f"com:{item['path']}:{c['id']}", "kind": "comment", "title": f"{c['by']} commented on {item['path']}",
                        "detail": c["text"][:120], "href": href(item["path"], "comments"), "at": c["at"]})
    return sorted((n for n in out if n["id"] not in dismissed), key=lambda n: n["at"], reverse=True)
