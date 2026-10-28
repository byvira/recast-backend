"""The Ops prompt library end to end: what the product reads, the rules the server enforces, and every route.

The product must never get worse because of an Ops edit: a published version applies only when switched on for its group,
never to a Dev prompt, and any problem with it falls back to the file. Every write is checked on the server by role.
"""

import pytest

from app.db.mongo import get_db, users
from app.prompt_library import deploy_check, live, ops_lists, rules, service
from app.prompts import registry
from app.prompts.registry import load_prompt, render_fragment, template_version

API = "/api/v1/ops/prompt-library"
CHIP = "text/refine/chips/shorten"
MASTER = "text/generate/master"
DEV = "media/image_no_text"
COLLECTIONS = ["prompt_state", "prompt_requests", "prompt_classification", "prompt_comments", "prompt_suites", "prompt_ops_data",
               "prompt_library_staff", "prompt_folders", "prompt_proposed", "prompt_notice_dismissals", "prompt_library_audit",
               "prompt_library_settings", "prompt_fallbacks"]


@pytest.fixture(autouse=True)
async def _clean_library():
    db = get_db()
    for name in COLLECTIONS:
        await db[name].delete_many({})
    live.set_snapshot(live.Snapshot())
    live.clear_fallbacks()
    live.forget_failures()
    yield
    live.set_snapshot(live.Snapshot())
    live.clear_fallbacks()
    live.forget_failures()


def _snap(versions: dict, groups=("quick",), enabled=True, ops_data=None) -> live.Snapshot:
    return live.Snapshot(enabled=enabled, groups=frozenset(groups), versions=versions, ops_data=ops_data or {})


def _file(path: str, **variables) -> str:
    return registry._file_env.get_template(f"{path}.jinja").render(**variables)


# ---------------------------------------------------------------------------------------------------------------------
# What the product reads
# ---------------------------------------------------------------------------------------------------------------------

def test_with_nothing_published_every_prompt_is_exactly_its_file():
    assert load_prompt(CHIP) == _file(CHIP)
    assert template_version(CHIP).count("+") == 0


def test_a_published_version_is_used_only_when_switched_on_for_its_group():
    published = {CHIP: {"number": 3, "texts": {"all": "Cut it in half. Keep the hook.\n"}}}
    live.set_snapshot(_snap(published))
    assert load_prompt(CHIP) == "Cut it in half. Keep the hook.\n"
    assert template_version(CHIP).endswith("+v3")
    live.set_snapshot(_snap(published, groups=("platform",)))
    assert load_prompt(CHIP) == _file(CHIP)
    live.set_snapshot(_snap(published, enabled=False))
    assert load_prompt(CHIP) == _file(CHIP)


def test_a_dev_prompt_never_takes_a_published_version():
    live.set_snapshot(_snap({DEV: {"number": 1, "texts": {"whole": "Anything.\n"}}}, groups=live.GROUP_IDS))
    assert load_prompt(DEV) == _file(DEV)


def test_a_broken_published_version_falls_back_to_the_file_and_is_counted():
    live.set_snapshot(_snap({CHIP: {"number": 2, "texts": {"all": "Use {{ not_passed }} here.\n"}}}))
    assert load_prompt(CHIP) == _file(CHIP)
    assert live.fallbacks()[CHIP]["count"] == 1 and live.fallbacks()[CHIP]["version"] == "v2"
    live.set_snapshot(_snap({CHIP: {"number": 3, "texts": {"all": "{% if %}\n"}}}))
    assert load_prompt(CHIP) == _file(CHIP)


def test_text_for_a_section_the_file_no_longer_has_is_ignored():
    live.set_snapshot(_snap({CHIP: {"number": 1, "texts": {"removed_section": "Old text.\n"}}}))
    assert load_prompt(CHIP) == _file(CHIP)


def test_a_missing_variable_the_file_needs_still_reaches_the_caller():
    live.set_snapshot(_snap({"text/hooks/score": {"number": 1, "texts": {}}}, groups=live.GROUP_IDS))
    with pytest.raises(Exception):
        load_prompt("text/hooks/score")


def test_only_editable_sections_change_and_locked_text_stays_as_written():
    regions = [r for r in service.file_prompt(MASTER).regions if r.kind == "editable"]
    first = regions[0]
    live.set_snapshot(_snap({MASTER: {"number": 1, "texts": {first.id: "CHECK BEFORE YOU ANSWER.\n", "locked_1": "Hacked.\n"}}},
                            groups=("core",)))
    values = {n: "X" for n in ["language_instruction", "brand_context", "tone_override_text", "language_short"]}
    try:
        text = load_prompt(MASTER, **values)
    except Exception:  # the master prompt needs more variables; check the composed source instead
        text = live.compose_live(MASTER, service.file_prompt(MASTER).text)[0]
    assert "CHECK BEFORE YOU ANSWER." in text and "Hacked." not in text


def test_fragments_take_published_versions_too():
    path = "text/refine/chips/expand"
    live.set_snapshot(_snap({path: {"number": 1, "texts": {"all": "Make it longer with one real example.\n"}}}))
    assert render_fragment(path) == "Make it longer with one real example.\n"


def test_ops_lists_are_the_code_values_until_published_and_switched_on():
    assert ops_lists.words("generic_closings") == ops_lists._code_words("generic_closings")
    assert ops_lists.tone_instructions()["formal"] == ops_lists._text_fixture()["tone_instructions"]["formal"]
    data = {
        "tone_options": {"number": 1, "data": [{"id": "formal", "label": "Formal", "instruction": "Write very formally please.", "enabled": True},
                                               {"id": "witty", "label": "Witty", "instruction": "Be witty in every line here.", "enabled": False}]},
        "quality_words": {"number": 1, "data": [{"language": "en", "list": "generic_closings", "label": "x", "words": ["see you soon"]}]},
        "content_goals": {"number": 1, "data": [{"id": "educate", "text": "Teach one useful thing clearly."}]},
        "register_notes": {"number": 1, "data": [{"code": "hi", "language": "Hindi", "note": "Everyday Hindi."}]},
    }
    live.set_snapshot(_snap({}, groups=("quick",), ops_data=data))
    assert ops_lists.tone_instructions()["formal"] == ops_lists._text_fixture()["tone_instructions"]["formal"]
    live.set_snapshot(_snap({}, groups=("lists",), ops_data=data))
    tones = ops_lists.tone_instructions()
    assert tones["formal"] == "Write very formally please." and tones["witty"] == "Be witty in every line here."
    # a tone missing from the published list keeps its code wording, so nobody who chose it gets no tone at all
    assert tones["casual"] == ops_lists._text_fixture()["tone_instructions"]["casual"]
    assert ops_lists.shown_tones() == ["formal"]
    assert ops_lists.words("generic_closings") == ["see you soon"]
    assert ops_lists.words("generic_openings") == ops_lists._code_words("generic_openings")
    assert ops_lists.goal_instructions() == {"educate": "Teach one useful thing clearly."}
    assert ops_lists.register_note("hi") == "Everyday Hindi." and ops_lists.register_note("ta") == ""

    from app.pipelines.text.brand_context import build_goal_context, build_tone_override
    assert build_tone_override("formal", "en") == "Write very formally please.\n\n"
    assert build_goal_context("educate") == "Teach one useful thing clearly.\n\n"


def test_the_code_values_for_every_list_pass_their_own_checks():
    for kind in ops_lists.KINDS:
        assert rules.validate_ops_data(kind, ops_lists.code_defaults(kind)) == [], kind


def test_every_prompt_belongs_to_a_switch_group():
    for path in service.file_paths():
        assert live.group_of(path) in live.GROUP_IDS
    assert live.group_of("content_guard/screen") == "core" and live.group_of(MASTER) == "core"
    assert live.group_of(CHIP) == "quick" and live.group_of("text/generate/platform_rules/linkedin") == "platform"


# ---------------------------------------------------------------------------------------------------------------------
# Routes, roles and the workflow
# ---------------------------------------------------------------------------------------------------------------------

async def _person(signup_user, *, staff=False, master=False):
    client, profile = await signup_user()
    fields = {}
    if staff:
        fields["is_platform_staff"] = True
    if master:
        fields["is_master_admin"] = True
    if fields:
        await users.update_one({"id": profile["id"]}, {"$set": fields})
    return client, profile


async def _sign_off(client, path):
    res = await client.post(f"{API}/classification/sign-off", json={"path": path})
    assert res.status_code == 200, res.text


async def _draft_and_check(client, path, edits, revision=0):
    res = await client.put(f"{API}/draft", json={"path": path, "edits": edits, "revision": revision})
    assert res.status_code == 200, res.text
    res = await client.post(f"{API}/checks", json={"path": path})
    assert res.status_code == 200, res.text
    return res.json()


async def test_only_platform_staff_reach_the_library_and_unlisted_staff_can_draft_but_not_publish(signup_user):
    member, _ = await _person(signup_user)
    assert (await member.get(f"{API}/index")).status_code == 403
    staff, _ = await _person(signup_user, staff=True)
    session = (await staff.get(f"{API}/session")).json()
    assert session["role"] == "editor"
    await _draft_and_check(staff, CHIP, {"all": "Cut this to half its length. Keep the hook and the closing.\n"})
    res = await staff.post(f"{API}/publish", json={"path": CHIP, "note": "Shorter wording"})
    assert res.status_code == 403 and res.json()["detail"]["code"] == "forbidden"
    assert (await staff.post(f"{API}/staff", json={"email": "a@b.co", "role": "editor"})).status_code == 403
    assert (await staff.put(f"{API}/switches", json={"enabled": True, "groups": ["quick"]})).status_code == 403


async def test_publish_goes_live_for_real_and_rollback_and_revert_add_versions(signup_user):
    admin, _ = await _person(signup_user, master=True)
    assert (await admin.get(f"{API}/session")).json()["role"] == "admin"
    blocked = await admin.put(f"{API}/draft", json={"path": CHIP, "edits": {"all": "Cut it in half.\n"}, "revision": 0})
    assert blocked.status_code == 200
    await admin.post(f"{API}/checks", json={"path": CHIP})
    not_signed = await admin.post(f"{API}/publish", json={"path": CHIP, "note": "First Ops wording"})
    assert not_signed.status_code == 400 and "Sign off" in not_signed.json()["detail"]["message"]
    await _sign_off(admin, CHIP)
    published = await admin.post(f"{API}/publish", json={"path": CHIP, "note": "First Ops wording"})
    assert published.status_code == 200, published.text
    assert published.json()["versions"][0]["status"] == "live"

    assert load_prompt(CHIP) == _file(CHIP), "nothing changes until the group is switched on"
    switched = await admin.put(f"{API}/switches", json={"enabled": True, "groups": ["quick"]})
    assert switched.status_code == 200 and switched.json()["groups"] == ["quick"]
    assert load_prompt(CHIP) == "Cut it in half.\n"

    back = await admin.post(f"{API}/revert", json={"path": CHIP, "regionIds": ["all"], "reason": "Back to the code text"})
    assert back.status_code == 200 and [v["number"] for v in back.json()["versions"]] == [2, 1]
    assert load_prompt(CHIP) == _file(CHIP)
    again = await admin.post(f"{API}/rollback", json={"path": CHIP, "toNumber": 1, "reason": "The short one was better"})
    assert again.status_code == 200 and again.json()["versions"][0]["restoredFrom"] == 1
    assert load_prompt(CHIP) == "Cut it in half.\n"

    index = {r["path"]: r for r in (await admin.get(f"{API}/index")).json()}
    assert index[CHIP]["status"] == "live_override" and index[CHIP]["overrideVersion"] == 3 and index[CHIP]["signedOff"]
    actions = [a["action"] for a in (await admin.get(f"{API}/audit", params={"path": CHIP})).json()]
    for action in ("override.publish", "override.rollback", "override.revert_to_default", "classification.signoff", "test.run"):
        assert action in actions


async def test_drafts_clash_safely_and_locked_or_dev_text_cannot_be_drafted(signup_user):
    admin, _ = await _person(signup_user, master=True)
    ok = await admin.put(f"{API}/draft", json={"path": CHIP, "edits": {"all": "One.\n"}, "revision": 0})
    assert ok.json()["draft"]["revision"] == 1
    clash = await admin.put(f"{API}/draft", json={"path": CHIP, "edits": {"all": "Two.\n"}, "revision": 0})
    assert clash.status_code == 409 and clash.json()["detail"]["code"] == "conflict"
    locked = await admin.put(f"{API}/draft", json={"path": MASTER, "edits": {"locked_1": "x\n"}, "revision": 0})
    assert locked.status_code == 400
    dev = await admin.put(f"{API}/draft", json={"path": DEV, "edits": {"whole": "x\n"}, "revision": 0})
    assert dev.status_code == 403
    gone = await admin.put(f"{API}/draft", json={"path": CHIP, "edits": {"nope": "x\n"}, "revision": 1})
    assert gone.status_code == 400


async def test_failing_checks_stop_a_publish_unless_a_publisher_gives_a_real_reason(signup_user):
    admin, _ = await _person(signup_user, master=True)
    await _sign_off(admin, MASTER)
    editable = [r for r in service.file_prompt(MASTER).regions if r.kind == "editable"][0]
    results = await _draft_and_check(admin, MASTER, {editable.id: "Use {{ something_else }} here.\n"})
    assert any(r["id"] == "variables" and r["status"] == "fail" for r in results)
    refused = await admin.post(f"{API}/publish", json={"path": MASTER, "note": "Try a variable", "overrideReason": "short"})
    assert refused.status_code == 400


async def test_a_safety_prompt_needs_a_second_person_and_a_changed_draft_voids_the_approval(signup_user):
    first, _ = await _person(signup_user, master=True)
    second, _ = await _person(signup_user, master=True)
    await _sign_off(first, MASTER)
    editable = [r for r in service.file_prompt(MASTER).regions if r.kind == "editable"]
    body = editable[-1].body.replace("Do not explain.", "Do not explain anything.")
    await _draft_and_check(first, MASTER, {editable[-1].id: body})
    waiting = await first.post(f"{API}/publish", json={"path": MASTER, "note": "Clearer last line"})
    assert waiting.status_code == 400 and "approval" in waiting.json()["detail"]["message"].lower()
    asked = await first.post(f"{API}/publish-request", json={"path": MASTER, "note": "Clearer last line"})
    assert asked.status_code == 200 and asked.json()["byMe"] is True
    own = await first.post(f"{API}/publish-request/approve", json={"path": MASTER})
    assert own.status_code == 400
    approved = await second.post(f"{API}/publish-request/approve", json={"path": MASTER})
    assert approved.status_code == 200 and approved.json()["byMe"] is False
    detail = (await first.get(f"{API}/prompt", params={"path": MASTER})).json()
    assert detail["publishRequest"]["approvedBy"] and detail["publishRequest"]["byMe"] is True
    # once someone other than the person who asked approved, a publisher (here the approver) can publish
    done = await second.post(f"{API}/publish", json={"path": MASTER, "note": "Clearer last line"})
    assert done.status_code == 200, done.text
    assert done.json()["versions"][0]["approvedBy"]


async def test_the_deploy_check_flags_changed_defaults_and_removed_sections_and_resolving_adds_a_version(signup_user):
    admin, _ = await _person(signup_user, master=True)
    await _sign_off(admin, CHIP)
    await _draft_and_check(admin, CHIP, {"all": "Cut it in half.\n"})
    assert (await admin.post(f"{API}/publish", json={"path": CHIP, "note": "First Ops wording"})).status_code == 200
    db = get_db()
    await db.prompt_state.update_one({"_id": CHIP}, {"$set": {"defaults.all": "An older code default.\n", "repoHash": "old",
                                                               "versions.0.regionTexts.removed_part": "Old.\n"}})
    found = await deploy_check.check(db)
    assert found["changed"] == 1 and found["orphaned"] == 1
    entry = {r["path"]: r for r in (await admin.get(f"{API}/index")).json()}[CHIP]
    assert "repo_default_changed" in entry["attention"] and "orphaned_override" in entry["attention"]
    notes = (await admin.get(f"{API}/notifications")).json()
    assert {n["kind"] for n in notes} >= {"code_changed", "orphaned"}
    cleared = await admin.post(f"{API}/orphans/clear", json={"path": CHIP})
    assert cleared.json()["versions"][0]["number"] == 2 and "removed_part" not in cleared.json()["versions"][0]["regionTexts"]
    adopted = await admin.post(f"{API}/code-change", json={"path": CHIP, "choice": "adopt_default"})
    assert adopted.json()["versions"][0]["number"] == 3 and adopted.json()["repoChange"] is None
    # taking the new default removes the section's own text, so it follows the code from now on
    assert "all" not in adopted.json()["versions"][0]["regionTexts"]
    assert [v["status"] for v in adopted.json()["versions"]].count("live") == 1


async def test_proposed_prompts_and_folders_live_in_the_library_until_a_file_exists(signup_user):
    admin, _ = await _person(signup_user, master=True)
    folder = await admin.post(f"{API}/folders", json={"parent": "text", "name": "drafts_lab"})
    assert folder.status_code == 200 and folder.json()["path"] == "text/drafts_lab"
    made = await admin.post(f"{API}/prompts", json={"folder": "text/drafts_lab", "name": "idea_one", "reason": "Trying a new idea",
                                                     "text": "Write one specific opening line for this post."})
    assert made.status_code == 200
    index = {r["path"]: r for r in (await admin.get(f"{API}/index")).json()}
    assert index["text/drafts_lab/idea_one"]["proposed"] is True
    twice = await admin.post(f"{API}/prompts", json={"folder": "text/drafts_lab", "name": "idea_one", "reason": "Trying again here",
                                                      "text": "Write one specific opening line for this post."})
    assert twice.status_code == 400
    db = get_db()
    await db.prompt_proposed.insert_one({"_id": CHIP, "reason": "Already a file", "text": "x"})
    assert (await deploy_check.check(db))["linked"] == 1
    assert await db.prompt_proposed.find_one({"_id": CHIP}) is None


async def test_the_access_list_keeps_an_admin_and_roles_take_effect(signup_user):
    admin, _ = await _person(signup_user, master=True)
    staff, profile = await _person(signup_user, staff=True)
    listed = (await admin.get(f"{API}/staff")).json()
    assert any(s["role"] == "admin" for s in listed)
    added = await admin.post(f"{API}/staff", json={"email": profile["email"], "name": "Pat", "role": "publisher"})
    assert added.status_code == 200
    assert (await staff.get(f"{API}/session")).json()["role"] == "publisher"
    me = next(s for s in added.json() if s["role"] == "admin")
    admins = [s for s in added.json() if s["role"] == "admin"]
    if len(admins) == 1:
        assert (await admin.delete(f"{API}/staff/{me['id']}")).status_code == 400
        assert (await admin.put(f"{API}/staff/{me['id']}", json={"role": "editor"})).status_code == 400
    pat = next(s for s in added.json() if s["email"] == profile["email"])
    assert (await admin.delete(f"{API}/staff/{pat['id']}")).status_code == 200
    assert (await staff.get(f"{API}/session")).json()["role"] == "editor"


async def test_comments_notify_the_others_and_dismissing_keeps_them_gone(signup_user):
    first, _ = await _person(signup_user, master=True)
    second, _ = await _person(signup_user, master=True)
    res = await first.post(f"{API}/comments", json={"path": CHIP, "text": "Should this keep the closing line?", "regionId": "all"})
    assert res.status_code == 200 and res.json()[0]["by"]
    notes = (await second.get(f"{API}/notifications")).json()
    mine = [n for n in notes if n["kind"] == "comment"]
    assert mine and not [n for n in (await first.get(f"{API}/notifications")).json() if n["kind"] == "comment"]
    await second.post(f"{API}/notifications/dismiss", json={"ids": [mine[0]["id"]]})
    assert not [n for n in (await second.get(f"{API}/notifications")).json() if n["kind"] == "comment"]
    resolved = await second.post(f"{API}/comments/resolve", json={"path": CHIP, "id": res.json()[0]["id"], "resolved": True})
    assert resolved.json()[0]["resolvedBy"]


async def test_suites_start_with_cases_run_without_a_model_and_reject_bad_cases(signup_user):
    admin, _ = await _person(signup_user, master=True)
    suite = (await admin.get(f"{API}/suite", params={"path": CHIP})).json()
    assert [c["id"] for c in suite["cases"]][:2] == ["case_1", "case_2"]
    run = await admin.post(f"{API}/suite/run", json={"path": CHIP})
    assert run.status_code == 200 and all(r["status"] == "pass" for r in run.json()["results"])
    bad = await admin.put(f"{API}/suite", json={"path": CHIP, "cases": [{"id": "case_1", "name": "x", "values": {}, "assertions": []}]})
    assert bad.status_code == 400
    overview = (await admin.get(f"{API}/suites")).json()
    assert {r["path"]: r for r in overview}[CHIP]["passed"] >= 2


async def test_ops_lists_publish_roll_back_and_reach_the_product_when_switched_on(signup_user):
    admin, _ = await _person(signup_user, master=True)
    doc = (await admin.get(f"{API}/ops-data/tone_options")).json()
    assert doc["versions"] == [] and doc["codeDefault"][2]["label"] == "Bold"
    tones = [dict(t) for t in doc["codeDefault"]]
    tones[0]["instruction"] = "Write in a formal register with full sentences."
    tones[-1]["enabled"] = False
    assert (await admin.put(f"{API}/ops-data/tone_options/draft", json={"data": tones})).status_code == 200
    assert (await admin.post(f"{API}/ops-data/tone_options/publish", json={"note": "short"})).status_code == 400
    assert (await admin.post(f"{API}/ops-data/tone_options/publish", json={"note": "Formal tone clearer"})).status_code == 200
    await admin.put(f"{API}/switches", json={"enabled": True, "groups": ["lists"]})
    assert ops_lists.tone_instructions()["formal"] == "Write in a formal register with full sentences."
    shown = await admin.get("/api/v1/prompt-lists/tones")
    assert shown.status_code == 200 and tones[-1]["id"] not in shown.json()["shown"]
    back = await admin.post(f"{API}/ops-data/tone_options/revert", json={"reason": "Back to code for now"})
    assert back.status_code == 200
    assert ops_lists.tone_instructions()["formal"] == ops_lists._text_fixture()["tone_instructions"]["formal"]
    again = await admin.post(f"{API}/ops-data/tone_options/rollback", json={"toNumber": 1, "reason": "Formal was better"})
    assert again.status_code == 200
    bad = await admin.put(f"{API}/ops-data/content_goals/draft", json={"data": [{"id": "educate", "text": "{{ x }} short"}]})
    assert bad.status_code == 200
    assert (await admin.post(f"{API}/ops-data/content_goals/publish", json={"note": "Broken goal text"})).status_code == 400


def test_a_brand_default_tone_of_bold_gets_the_punchy_wording():
    from app.pipelines.text.brand_context import build_tone_override

    assert build_tone_override("bold", "en") == build_tone_override("punchy", "en") != ""


def test_no_prompt_is_rendered_once_at_start_up_so_every_published_version_can_reach_the_product():
    """A prompt rendered while the app loads is kept forever and would ignore a published version."""
    import importlib
    import sys

    seen = []
    original = registry._render
    registry._render = lambda name, variables: (seen.append(name), original(name, variables))[1]
    try:
        for module in [m for m in list(sys.modules) if m.startswith("app.pipelines.text.generator")]:
            importlib.reload(sys.modules[module])
    finally:
        registry._render = original
    assert seen == []


def test_platform_call_to_action_and_hashtag_rules_take_published_versions():
    from app.models.text import Platform
    from app.pipelines.text.generator import CTA_RULES, HASHTAG_RULES, PLATFORM_RULES

    assert len(PLATFORM_RULES) == len(CTA_RULES) == len(HASHTAG_RULES) == len(Platform)
    live.set_snapshot(_snap({"text/generate/cta_rules/blog": {"number": 1, "texts": {"all": "One clear next step.\n"}}}))
    assert CTA_RULES.get(Platform.BLOG) == "One clear next step.\n"
    assert HASHTAG_RULES.get(Platform.BLOG) == _file("text/generate/hashtag_rules/blog")



# ---------------------------------------------------------------------------------------------------------------------
# Found in the end-to-end review
# ---------------------------------------------------------------------------------------------------------------------

def test_published_text_is_sandboxed_so_it_can_never_reach_python_objects():
    attack = "{{ ''.__class__.__mro__[1].__subclasses__() | length }}\n"
    live.set_snapshot(_snap({CHIP: {"number": 7, "texts": {"all": attack}}}))
    assert load_prompt(CHIP) == _file(CHIP)
    assert not template_version(CHIP).endswith("+v7"), "a version that failed is not reported as used"
    # the failed version is not rebuilt on every call
    assert live.live_key(CHIP) is None


def test_the_checks_refuse_code_comments_and_unclosed_brackets_in_an_edit():
    from app.prompt_library.regions import parse_regions

    regions = parse_regions('{# @editable craft "Craft" #}\nWrite about {{ topic }}.\n{# @end #}\n')
    for bad in ["Write about {{ topic.__class__ }}.\n", "Write about {{ topic }}. {#\n", "Write about {{ topic }} and {{ more\n",
                "Write about {{ topic | upper }}.\n"]:
        results = {r["id"]: r for r in rules.run_free_checks(regions, {"craft": bad}, [], [])}
        assert results["template_logic"]["status"] == "fail", bad
    fine = {r["id"]: r for r in rules.run_free_checks(regions, {"craft": "Write well about {{ topic }} for {{ audience }}.\n"}, [],
                                                      ["audience"])}
    assert all(r["status"] == "pass" for r in fine.values())


def test_a_version_keeps_only_changed_sections_so_later_code_fixes_reach_the_product():
    from app.prompt_library.regions import parse_regions

    regions = parse_regions('{# @editable a "A" #}\nOne.\n{# @end #}\n{# @editable b "B" #}\nTwo.\n{# @end #}\n')
    state = rules.empty_state(regions, "h1")
    state = rules.save_draft(state, {"a": "One, edited.\n", "b": "Two.\n"}, 0, "Asha", "2026-10-27T00:00:00Z")[1]
    state = rules.record_checks(state, rules.run_free_checks(regions, state["draft"]["edits"], [], []))
    state = rules.publish(state, who="Asha", now="2026-10-27T00:00:00Z", change_note="Edited section a", approved_by=None,
                          override_reason=None, require_approval=False, allow_override=False)[1]
    assert rules.live_version(state)["regionTexts"] == {"a": "One, edited.\n"}
    fixed = parse_regions('{# @editable a "A" #}\nOne.\n{# @end #}\n{# @editable b "B" #}\nTwo, fixed in code.\n{# @end #}\n')
    state = rules.apply_repo_deploy(state, fixed, "h2", "2026-10-28T00:00:00Z")
    assert rules.effective_texts(state)["b"] == "Two, fixed in code.\n" and state["repoChange"] is None


def test_draft_revisions_only_go_up_so_an_old_screen_never_matches_a_newer_draft():
    from app.prompt_library.regions import parse_regions

    regions = parse_regions('{# @editable a "A" #}\nOne.\n{# @end #}\n')
    state = rules.empty_state(regions, "h")
    for i in range(3):
        state = rules.save_draft(state, {"a": f"v{i}\n"}, (state["draft"] or {}).get("revision", 0), "A", "t")[1]
    assert state["draft"]["revision"] == 3
    state = rules.discard_draft(state)
    state = rules.save_draft(state, {"a": "new\n"}, 0, "B", "t")[1]
    assert state["draft"]["revision"] == 4
    assert rules.save_draft(state, {"a": "stale\n"}, 3, "A", "t")[0] is False


def test_ops_word_lists_refuse_entries_that_would_fail_every_post():
    rows = lambda words: [{"language": "en", "list": "generic_closings", "label": "Closings", "words": words}]
    assert rules.validate_ops_data("quality_words", rows(["", "ok"]))
    assert rules.validate_ops_data("quality_words", rows(["a"]))
    assert rules.validate_ops_data("quality_words", rows(["Lets Connect"]))
    assert rules.validate_ops_data("quality_words", rows("abc"))
    assert rules.validate_ops_data("tone_options", [])
    assert rules.validate_ops_data("quality_words", rows(["let's connect"])) == []


def test_odd_prompt_paths_are_a_plain_not_found():
    assert service.file_prompt("../registry") is None and service.file_prompt("a\x00b") is None
    assert service.file_prompt("text/refine/chips/shorten") is not None
    assert service.file_prompt("fixtures/_test_fixture") is not None


async def test_odd_prompt_paths_get_404_not_500(signup_user):
    admin, _ = await _person(signup_user, master=True)
    for path in ["../prompts/registry", "text/../../app", "a\x00b", "/etc/passwd"]:
        res = await admin.get(f"{API}/prompt", params={"path": path})
        assert res.status_code == 404, (path, res.status_code)


async def test_publish_refuses_text_that_would_not_build(signup_user):
    admin, _ = await _person(signup_user, master=True)
    await _sign_off(admin, CHIP)
    await admin.put(f"{API}/draft", json={"path": CHIP, "edits": {"all": "Cut it. {{ }}\n"}, "revision": 0})
    await admin.post(f"{API}/checks", json={"path": CHIP})
    res = await admin.post(f"{API}/publish", json={"path": CHIP, "note": "Broken brackets", "overrideReason": "Testing a broken edit here"})
    assert res.status_code == 400


async def test_a_rollback_clears_an_old_publish_request(signup_user):
    admin, _ = await _person(signup_user, master=True)
    await _sign_off(admin, CHIP)
    await _draft_and_check(admin, CHIP, {"all": "Cut it in half.\n"})
    assert (await admin.post(f"{API}/publish", json={"path": CHIP, "note": "First Ops wording"})).status_code == 200
    await _draft_and_check(admin, CHIP, {"all": "Cut it by a third.\n"}, revision=0)
    assert (await admin.post(f"{API}/publish-request", json={"path": CHIP, "note": "Please check this"})).status_code == 200
    assert (await admin.post(f"{API}/rollback", json={"path": CHIP, "toNumber": 1, "reason": "Back to the first one"})).status_code == 200
    detail = (await admin.get(f"{API}/prompt", params={"path": CHIP})).json()
    assert detail["publishRequest"] is None


async def test_a_dismissed_code_change_notice_comes_back_for_a_new_change(signup_user):
    admin, _ = await _person(signup_user, master=True)
    await _sign_off(admin, CHIP)
    await _draft_and_check(admin, CHIP, {"all": "Cut it in half.\n"})
    await admin.post(f"{API}/publish", json={"path": CHIP, "note": "First Ops wording"})
    db = get_db()
    await db.prompt_state.update_one({"_id": CHIP}, {"$set": {"defaults.all": "Old.\n", "repoHash": "old"}})
    await deploy_check.check(db)
    first = [n for n in (await admin.get(f"{API}/notifications")).json() if n["kind"] == "code_changed"]
    await admin.post(f"{API}/notifications/dismiss", json={"ids": [first[0]["id"]]})
    assert not [n for n in (await admin.get(f"{API}/notifications")).json() if n["kind"] == "code_changed"]
    await admin.post(f"{API}/code-change", json={"path": CHIP, "choice": "keep_override"})
    await db.prompt_state.update_one({"_id": CHIP}, {"$set": {"defaults.all": "Older.\n", "repoHash": "older"}})
    await deploy_check.check(db)
    again = [n for n in (await admin.get(f"{API}/notifications")).json() if n["kind"] == "code_changed"]
    assert again and again[0]["id"] != first[0]["id"]


async def test_ops_list_saves_never_lose_a_publish(signup_user):
    import asyncio

    admin, _ = await _person(signup_user, master=True)
    doc = (await admin.get(f"{API}/ops-data/content_goals")).json()
    goals = [dict(g) for g in doc["codeDefault"]]
    goals[0]["text"] = "Teach one useful thing, clearly and simply."
    await admin.put(f"{API}/ops-data/content_goals/draft", json={"data": goals})
    results = await asyncio.gather(*[admin.post(f"{API}/ops-data/content_goals/publish", json={"note": f"Clearer goal {i} today"})
                                     for i in range(3)])
    assert sum(r.status_code == 200 for r in results) == 1
    versions = (await admin.get(f"{API}/ops-data/content_goals")).json()["versions"]
    assert [v["number"] for v in versions] == [1]
