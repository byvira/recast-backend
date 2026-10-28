"""The Ops prompt library: every action the Prompts screens offer, saved for real and checked on the server.

Platform staff only. Each person's library role (editor, publisher, admin) comes from the access list; master admins are
always admin. The rules live in app/prompt_library/rules.py and match the ones the screens show.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.auth import get_current_user, require_platform_staff
from app.core.middleware import limiter
from app.db.mongo import get_db
from app.prompt_library import live, ops_lists, rules, service
from app.prompt_library.regions import compose, find_slots
from app.prompt_library.service import Actor, LibraryError, Prompt, must

router = APIRouter()
lists_router = APIRouter()

_STATUS = {"conflict": 409, "forbidden": 403, "invalid": 400, "not_found": 404}
FALLBACK_HOURS = 24


def library_error_response(_: Request, exc: LibraryError) -> JSONResponse:
    return JSONResponse(status_code=_STATUS.get(exc.code, 400), content={"detail": {"code": exc.code, "message": exc.message}})


async def _actor(user: dict = Depends(require_platform_staff)) -> Actor:
    return await service.actor_for(get_db(), user)


# ---------------------------------------------------------------------------------------------------------------------
# Building what the screens read
# ---------------------------------------------------------------------------------------------------------------------

def _signed_off(prompt: Prompt, cls: dict | None) -> bool:
    return bool(cls and cls.get("signedOffBy") and cls.get("signedOffKlass") == prompt.klass)


def _now_ms() -> float:
    return time.time() * 1000


def _attention(prompt: Prompt, state: dict, signed: bool, fallback: dict | None) -> list[str]:
    flags = rules.attention_flags(state, signed, _now_ms())
    live_v = rules.live_version(state)
    if fallback and live_v and fallback.get("version") == f"v{live_v['number']}":
        flags.append("live_fallback")
    return flags


def _index_entry(prompt: Prompt, state: dict, cls: dict | None, fallback: dict | None = None) -> dict:
    regions = prompt.regions
    live_v = rules.live_version(state)
    stamps = sorted(s for s in [(state.get("draft") or {}).get("savedAt"), live_v["publishedAt"] if live_v else None] if s)
    signed = _signed_off(prompt, cls)
    return {
        "path": prompt.path, "folder": prompt.folder, "name": prompt.name, "klass": prompt.klass, "reason": prompt.reason,
        "safetySensitive": prompt.safety_sensitive, "proposed": prompt.proposed, "receives": prompt.receives,
        "receivesMore": prompt.receives_more, "lines": prompt.lines, "usedBy": prompt.used_by,
        "protectedLiterals": prompt.protected, "status": rules.override_status(state),
        "overrideVersion": live_v["number"] if live_v else None, "testsHealth": rules.tests_health(state),
        "attention": _attention(prompt, state, signed, fallback), "signedOff": signed,
        "updatedAt": stamps[-1] if stamps else None,
        "editableRegions": sum(1 for r in regions if r.kind == "editable"),
        "lockedRegions": sum(1 for r in regions if r.kind == "locked"),
        "group": live.group_of(prompt.path),
    }


def _classification_row(prompt: Prompt, cls: dict | None) -> dict:
    cls = cls or {}
    proposal = cls.get("proposedKlass")
    signed = _signed_off(prompt, cls)
    return {
        "path": prompt.path, "klass": prompt.klass, "reason": prompt.reason, "proposedKlass": proposal,
        "proposalNote": cls.get("proposalNote"), "regionsDefined": prompt.klass != "dev" and not proposal,
        "testsDefined": prompt.klass != "dev" and not proposal, "signedOffBy": cls.get("signedOffBy") if signed else None,
        "signedOffAt": cls.get("signedOffAt") if signed else None,
    }


def _must_build(prompt: Prompt, texts: dict[str, str]) -> None:
    """Refuses text that would not build into a prompt (the sandboxed loader the product uses for published text)."""
    from app.prompts.registry import _env

    editable = {r.id for r in prompt.regions if r.kind == "editable"}
    try:
        _env.from_string(compose(prompt.regions, {k: v for k, v in texts.items() if k in editable}))
    except Exception as exc:  # noqa: BLE001
        raise LibraryError("invalid", "This text doesn't build into a working prompt. Check the curly brackets and tags.") from exc


def _no_dev(prompt: Prompt) -> None:
    if prompt.klass == "dev":
        raise LibraryError("forbidden", "This prompt is set in code. A code change is needed to change it.")


def _request_view(req: dict | None, actor: Actor) -> dict | None:
    if not req:
        return None
    return {"by": req["by"], "at": req["at"], "note": req["note"], "draftRevision": req["draftRevision"],
            "approvedBy": req.get("approvedBy"), "approvedAt": req.get("approvedAt"), "byMe": req.get("byId") == actor.id}


async def _fallbacks(db) -> dict[str, dict]:
    since = time.time() - FALLBACK_HOURS * 3600
    return {d["_id"]: d async for d in db.prompt_fallbacks.find({"lastAt": {"$gte": since}})}


async def _index(db, with_states: bool = False):
    prompts = await service.catalog(db)
    states = await service.all_states(db)
    classes = {d["_id"]: d async for d in db.prompt_classification.find({})}
    falls = await _fallbacks(db)
    rows = [_index_entry(p, service._state_of(states.get(path), p), classes.get(path), falls.get(path))
            for path, p in prompts.items()]
    return (prompts, rows, states) if with_states else (prompts, rows)


# ---------------------------------------------------------------------------------------------------------------------
# Session, library, folders, new prompts
# ---------------------------------------------------------------------------------------------------------------------

@router.get("/session")
async def session(actor: Actor = Depends(_actor)) -> dict:
    return {"name": actor.name, "role": actor.role, "id": actor.id}


@router.get("/index")
@limiter.limit("120/minute")
async def list_index(request: Request, actor: Actor = Depends(_actor)) -> list[dict]:
    _, rows = await _index(get_db())
    return rows


class NewPrompt(BaseModel):
    folder: str = Field("", max_length=200)
    name: str = Field("", max_length=80)
    reason: str = Field("", max_length=600)
    text: str = Field("", max_length=20000)


@router.post("/prompts")
@limiter.limit("30/minute")
async def create_prompt(request: Request, body: NewPrompt, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "create_prompt")
    prompts = await service.catalog(db)
    issues = rules.validate_new_prompt(body.model_dump(), set(prompts), await service.all_folders(db, prompts))
    if issues:
        raise LibraryError("invalid", issues[0]["message"])
    folder = body.folder.strip().strip("/")
    path = f"{folder}/{body.name.strip()}"
    try:
        await db.prompt_proposed.insert_one({"_id": path, "reason": body.reason.strip(), "text": rules.wrap_new_prompt(body.text),
                                             "createdBy": actor.name, "createdAt": service.now_iso()})
    except Exception as exc:  # noqa: BLE001 - a duplicate means someone made it a moment ago
        raise LibraryError("conflict", "A prompt with that name already exists in this folder.") from exc
    await service.audit(db, actor, "prompt.create", path, "New prompt, no code reads it yet", body.reason.strip())
    return {"path": path}


@router.get("/folders")
async def list_folders(actor: Actor = Depends(_actor)) -> list[str]:
    return sorted([d["_id"] async for d in get_db().prompt_folders.find({}, {"_id": 1})])


class NewFolder(BaseModel):
    parent: str = Field("", max_length=200)
    name: str = Field("", max_length=80)


@router.post("/folders")
@limiter.limit("30/minute")
async def create_folder(request: Request, body: NewFolder, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "create_prompt")
    issues = rules.validate_new_folder(body.model_dump(), await service.all_folders(db))
    if issues:
        raise LibraryError("invalid", issues[0]["message"])
    path = rules.new_folder_path(body.model_dump())
    try:
        await db.prompt_folders.insert_one({"_id": path, "createdBy": actor.name, "createdAt": service.now_iso()})
    except Exception as exc:  # noqa: BLE001
        raise LibraryError("conflict", "A folder with that name already exists here.") from exc
    await service.audit(db, actor, "folder.create", path, "New empty folder")
    return {"path": path}


@router.get("/prompt")
async def get_prompt(path: str = Query(..., max_length=300), actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    prompt = await service.find(db, path)
    state = await service.get_state(db, prompt)
    cls = await db.prompt_classification.find_one({"_id": path})
    falls = await _fallbacks(db)
    regions = prompt.regions
    return {
        **_index_entry(prompt, state, cls, falls.get(path)),
        "text": prompt.text,
        "regions": [r.as_dict() for r in regions],
        "slots": find_slots(compose(regions)),
        "state": state,
        "classification": _classification_row(prompt, cls),
        "publishRequest": _request_view(await db.prompt_requests.find_one({"_id": path}), actor),
    }


# ---------------------------------------------------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------------------------------------------------

@router.get("/classification")
async def list_classification(actor: Actor = Depends(_actor)) -> list[dict]:
    db = get_db()
    prompts = await service.catalog(db)
    classes = {d["_id"]: d async for d in db.prompt_classification.find({})}
    return [_classification_row(p, classes.get(path)) for path, p in prompts.items()]


class Proposal(BaseModel):
    path: str = Field(..., max_length=300)
    klass: str = Field(..., pattern="^(ops|hybrid|dev)$")
    note: str = Field("", max_length=1000)


@router.post("/classification/propose")
async def propose_class(body: Proposal, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "propose_class")
    prompt = await service.find(db, body.path)
    if not body.note.strip():
        raise LibraryError("invalid", "Add a note saying why.")
    await db.prompt_classification.update_one(
        {"_id": body.path},
        {"$set": {"proposedKlass": body.klass, "proposalNote": body.note.strip()},
         "$unset": {"signedOffBy": "", "signedOffAt": "", "signedOffKlass": ""}},
        upsert=True,
    )
    await service.audit(db, actor, "classification.propose", body.path, f"{prompt.klass} to {body.klass}", body.note.strip())
    return _classification_row(prompt, await db.prompt_classification.find_one({"_id": body.path}))


class PathOnly(BaseModel):
    path: str = Field(..., max_length=300)


@router.post("/classification/sign-off")
async def sign_off(body: PathOnly, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "sign_off")
    prompt = await service.find(db, body.path)
    cls = await db.prompt_classification.find_one({"_id": body.path}) or {}
    if cls.get("proposedKlass"):
        raise LibraryError("invalid", "Define the locked regions and the tests first, then sign this off.")
    await db.prompt_classification.update_one(
        {"_id": body.path},
        {"$set": {"signedOffBy": actor.name, "signedOffById": actor.id, "signedOffAt": service.now_iso(), "signedOffKlass": prompt.klass}},
        upsert=True,
    )
    await service.audit(db, actor, "classification.signoff", body.path, prompt.klass)
    return _classification_row(prompt, await db.prompt_classification.find_one({"_id": body.path}))


@router.get("/audit")
async def list_audit(path: str | None = Query(None, max_length=300), actor: Actor = Depends(_actor)) -> list[dict]:
    return await service.list_audit(get_db(), path)


# ---------------------------------------------------------------------------------------------------------------------
# Drafts, checks, approval, publishing
# ---------------------------------------------------------------------------------------------------------------------

class DraftBody(BaseModel):
    path: str = Field(..., max_length=300)
    edits: dict[str, str]
    revision: int = Field(..., ge=0)


@router.put("/draft")
@limiter.limit("240/minute")
async def save_draft(request: Request, body: DraftBody, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "edit_draft")
    prompt = await service.find(db, body.path)
    if prompt.klass == "dev":
        raise LibraryError("forbidden", "This prompt is set in code. A code change is needed to change it.")
    by_id = {r.id: r for r in prompt.regions}
    for rid, text in body.edits.items():
        if rid not in by_id:
            raise LibraryError("invalid", "That section no longer exists. Reload and try again.")
        if by_id[rid].kind == "locked":
            raise LibraryError("invalid", "That part is locked. It's set in code.")
        if len(text) > rules.MAX_REGION_CHARS * 2:
            raise LibraryError("invalid", f"Keep this section under {rules.MAX_REGION_CHARS} characters.")
    now = service.now_iso()
    state = await service.update_state(
        db, prompt, lambda s: must(rules.save_draft(s, body.edits, body.revision, actor.name, now), "conflict"))
    await db.prompt_requests.delete_one({"_id": body.path})
    if body.revision == 0:
        await service.audit(db, actor, "override.draft.save", body.path, "Draft started")
    return state


@router.delete("/draft")
async def discard_draft(path: str = Query(..., max_length=300), actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "edit_draft")
    prompt = await service.find(db, path)
    state = await service.update_state(db, prompt, rules.discard_draft)
    await db.prompt_requests.delete_one({"_id": path})
    return state


@router.post("/checks")
async def run_checks(body: PathOnly, actor: Actor = Depends(_actor)) -> list[dict]:
    db = get_db()
    service.require(actor, "run_checks")
    prompt = await service.find(db, body.path)
    results: dict = {}

    def apply(state: dict) -> dict:
        if not state["draft"]:
            raise LibraryError("invalid", "There are no unpublished changes to test.")
        results["value"] = rules.run_free_checks(prompt.regions, state["draft"]["edits"], prompt.protected, prompt.extra_slots)
        return rules.record_checks(state, results["value"])

    await service.update_state(db, prompt, apply)
    failed = sum(1 for r in results["value"] if r["required"] and r["status"] == "fail")
    await service.audit(db, actor, "test.run", body.path,
                        f"{failed} required check{'' if failed == 1 else 's'} failed" if failed else "All required checks passed")
    return results["value"]


class NoteBody(BaseModel):
    path: str = Field(..., max_length=300)
    note: str = Field("", max_length=1000)


@router.post("/publish-request")
async def request_publish(body: NoteBody, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "request_publish")
    prompt = await service.find(db, body.path)
    state = await service.get_state(db, prompt)
    if not state["draft"]:
        raise LibraryError("invalid", "There are no unpublished changes.")
    if len(body.note.strip()) < rules.MIN_NOTE_CHARS:
        raise LibraryError("invalid", "Add a change note.")
    req = {"_id": body.path, "by": actor.name, "byId": actor.id, "at": service.now_iso(), "note": body.note.strip(),
           "draftRevision": state["draft"]["revision"], "approvedBy": None, "approvedById": None, "approvedAt": None}
    await db.prompt_requests.replace_one({"_id": body.path}, req, upsert=True)
    await service.audit(db, actor, "override.request", body.path, "Approval requested", body.note.strip())
    return _request_view(req, actor)


@router.post("/publish-request/approve")
async def approve_publish(body: PathOnly, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "approve")
    req = await db.prompt_requests.find_one({"_id": body.path})
    if not req:
        raise LibraryError("invalid", "Nobody has asked to publish this.")
    if req.get("byId") == actor.id:
        raise LibraryError("invalid", "A second person has to approve this. You made the request.")
    prompt = await service.find(db, body.path)
    state = await service.get_state(db, prompt)
    if not state["draft"] or state["draft"]["revision"] != req["draftRevision"]:
        raise LibraryError("invalid", "The draft changed after approval was asked. Ask for approval again.")
    update = {"approvedBy": actor.name, "approvedById": actor.id, "approvedAt": service.now_iso()}
    await db.prompt_requests.update_one({"_id": body.path}, {"$set": update})
    await service.audit(db, actor, "override.approve", body.path, f"Approved the request from {req['by']}")
    return _request_view({**req, **update}, actor)


class PublishBody(BaseModel):
    path: str = Field(..., max_length=300)
    note: str = Field("", max_length=1000)
    overrideReason: str | None = Field(None, max_length=1000)


@router.post("/publish")
@limiter.limit("30/minute")
async def publish(request: Request, body: PublishBody, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "publish")
    prompt = await service.find(db, body.path)
    if prompt.klass == "dev":
        raise LibraryError("forbidden", "This prompt is set in code. A code change is needed to change it.")
    cls = await db.prompt_classification.find_one({"_id": body.path})
    if not _signed_off(prompt, cls):
        raise LibraryError("invalid", "Sign off this prompt's class in Classification review before publishing it.")
    req = await db.prompt_requests.find_one({"_id": body.path})
    needs_approval = prompt.safety_sensitive
    now = service.now_iso()

    def apply(state: dict) -> dict:
        if needs_approval and req and state["draft"] and req["draftRevision"] != state["draft"]["revision"]:
            raise LibraryError("invalid", "The draft changed after approval was asked. Ask for approval again.")
        if state["draft"] and state["checks"] and not state["draft"]["checksStale"]:
            # The checks run again on the exact text being published, against the code as it is now, so a result saved
            # before a deploy can't let a broken edit through.
            state = rules.record_checks(
                state, rules.run_free_checks(prompt.regions, state["draft"]["edits"], prompt.protected, prompt.extra_slots))
        return must(rules.publish(
            state, who=actor.name, now=now, change_note=body.note,
            approved_by=(req or {}).get("approvedBy") if needs_approval else None,
            override_reason=body.overrideReason, require_approval=needs_approval,
            allow_override=rules.can(actor.role, "override_gate"),
            approver_is_requester=bool(needs_approval and req and req.get("approvedById") == req.get("byId")),
        ))

    current = await service.get_state(db, prompt)
    if current["draft"]:
        live_texts = (rules.live_version(current) or {}).get("regionTexts", {})
        _must_build(prompt, {**live_texts, **current["draft"]["edits"]})
    state = await service.update_state(db, prompt, apply)
    await db.prompt_requests.delete_one({"_id": body.path})
    version = state["versions"][0]
    overridden = version["gateResult"] == "overridden"
    await service.audit(db, actor, "override.publish.gate_overridden" if overridden else "override.publish", body.path,
                        f"v{version['number']}", body.overrideReason if overridden else body.note.strip())
    await db.prompt_fallbacks.delete_one({"_id": body.path})
    await service.after_publish(db)
    return state


class RollbackBody(BaseModel):
    path: str = Field(..., max_length=300)
    toNumber: int = Field(..., ge=1)
    reason: str = Field("", max_length=1000)


@router.post("/rollback")
async def rollback(body: RollbackBody, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "rollback")
    prompt = await service.find(db, body.path)
    _no_dev(prompt)
    now = service.now_iso()
    state = await service.update_state(db, prompt, lambda s: must(rules.rollback(s, body.toNumber, actor.name, now, body.reason)))
    await db.prompt_requests.delete_one({"_id": body.path})
    await service.audit(db, actor, "override.rollback", body.path, f"v{state['versions'][0]['number']} restores v{body.toNumber}",
                        body.reason.strip())
    await service.after_publish(db)
    return state


class RevertBody(BaseModel):
    path: str = Field(..., max_length=300)
    regionIds: list[str] = Field(..., max_length=100)
    reason: str = Field("", max_length=1000)


@router.post("/revert")
async def revert_to_default(body: RevertBody, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "rollback")
    prompt = await service.find(db, body.path)
    _no_dev(prompt)
    if not body.regionIds:
        raise LibraryError("invalid", "Choose at least one section.")
    now = service.now_iso()
    state = await service.update_state(
        db, prompt, lambda s: must(rules.revert_to_default(s, body.regionIds, actor.name, now, body.reason)))
    await service.audit(db, actor, "override.revert_to_default", body.path, ", ".join(body.regionIds), body.reason.strip())
    await service.after_publish(db)
    return state


class ChoiceBody(BaseModel):
    path: str = Field(..., max_length=300)
    choice: str = Field(..., pattern="^(keep_override|adopt_default)$")


@router.post("/code-change")
async def resolve_repo_change(body: ChoiceBody, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "publish")
    prompt = await service.find(db, body.path)
    _no_dev(prompt)
    now = service.now_iso()
    state = await service.update_state(db, prompt, lambda s: rules.resolve_repo_change(s, body.choice, actor.name, now))
    await service.audit(db, actor, "repo_default.resolve", body.path,
                        "Kept the override" if body.choice == "keep_override" else "Adopted the new default")
    await service.after_publish(db)
    return state


@router.post("/orphans/clear")
async def clear_orphans(body: PathOnly, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "publish")
    prompt = await service.find(db, body.path)
    gone: dict = {}
    now = service.now_iso()

    def apply(state: dict) -> dict:
        gone["ids"] = ", ".join(state["orphaned"])
        return rules.clear_orphans(state, actor.name, now)

    state = await service.update_state(db, prompt, apply)
    await service.audit(db, actor, "region.orphan.clear", body.path, gone.get("ids", ""))
    await service.after_publish(db)
    return state


# ---------------------------------------------------------------------------------------------------------------------
# Test suites
# ---------------------------------------------------------------------------------------------------------------------

def _cases_for(prompt: Prompt, saved: dict | None) -> list[dict]:
    if saved and saved.get("cases") is not None:
        return saved["cases"]
    return rules.suggested_cases(rules.sample_values(find_slots(compose(prompt.regions))), prompt.protected)


def _suite_run(prompt: Prompt, state: dict, cases: list[dict]) -> dict:
    texts = {**rules.effective_texts(state), **((state.get("draft") or {}).get("edits") or {})}
    return rules.run_suite(prompt.regions, texts, cases, "draft" if state.get("draft") else "live", service.now_iso())


def _overview_row(prompt: Prompt, saved: dict | None) -> dict:
    last = (saved or {}).get("last")
    s = rules.summarise(last["results"]) if last else None
    return {"path": prompt.path, "klass": prompt.klass, "cases": len(_cases_for(prompt, saved)),
            "passed": s["passed"] if s else 0, "failed": s["failed"] if s else 0,
            "ranAt": last["at"] if last else None, "against": last["against"] if last else None}


@router.get("/suite")
async def get_suite(path: str = Query(..., max_length=300), actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    prompt = await service.find(db, path)
    saved = await db.prompt_suites.find_one({"_id": path})
    return {"cases": _cases_for(prompt, saved), "last": (saved or {}).get("last")}


class SuiteBody(BaseModel):
    path: str = Field(..., max_length=300)
    cases: list[dict[str, Any]] = Field(..., max_length=100)


@router.put("/suite")
async def save_suite(body: SuiteBody, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "edit_draft")
    await service.find(db, body.path)
    problems = [p for c in body.cases for p in rules.validate_case(c)]
    if problems:
        raise LibraryError("invalid", problems[0])
    # Editing the cases makes the last run out of date, so it is dropped.
    await db.prompt_suites.replace_one({"_id": body.path}, {"_id": body.path, "cases": body.cases, "last": None}, upsert=True)
    return {"cases": body.cases, "last": None}


@router.post("/suite/run")
async def run_suite(body: PathOnly, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "run_checks")
    prompt = await service.find(db, body.path)
    saved = await db.prompt_suites.find_one({"_id": body.path})
    run = _suite_run(prompt, await service.get_state(db, prompt), _cases_for(prompt, saved))
    await db.prompt_suites.update_one({"_id": body.path}, {"$set": {"last": run}}, upsert=True)
    s = rules.summarise(run["results"])
    await service.audit(db, actor, "test.suite", body.path,
                        f"{s['failed']} of {s['total']} tests failed" if s["failed"] else f"All {s['total']} tests passed")
    return run


@router.get("/suites")
async def suite_overview(actor: Actor = Depends(_actor)) -> list[dict]:
    db = get_db()
    prompts = await service.catalog(db)
    saved = {d["_id"]: d async for d in db.prompt_suites.find({})}
    return [_overview_row(p, saved.get(path)) for path, p in prompts.items()]


@router.post("/suites/run")
@limiter.limit("10/minute")
async def run_all_suites(request: Request, actor: Actor = Depends(_actor)) -> list[dict]:
    db = get_db()
    service.require(actor, "run_checks")
    prompts = await service.catalog(db)
    states = await service.all_states(db)
    saved = {d["_id"]: d async for d in db.prompt_suites.find({})}
    failed = 0
    rows = []
    for path, prompt in prompts.items():
        run = _suite_run(prompt, service._state_of(states.get(path), prompt), _cases_for(prompt, saved.get(path)))
        failed += any(r["status"] == "fail" for r in run["results"])
        await db.prompt_suites.update_one({"_id": path}, {"$set": {"last": run}}, upsert=True)
        rows.append(_overview_row(prompt, {**(saved.get(path) or {}), "last": run}))
    await service.audit(db, actor, "test.suite", "all",
                        f"{failed} of {len(prompts)} prompts have failing tests" if failed else f"All {len(prompts)} prompts passed")
    return rows


# ---------------------------------------------------------------------------------------------------------------------
# Ops data lists
# ---------------------------------------------------------------------------------------------------------------------

_KIND_TITLE = {"register_notes": "Language register notes", "tone_options": "Tone options", "content_goals": "Content goals",
               "quality_words": "Quality word lists"}


@router.get("/ops-data/{kind}")
async def get_ops_data(kind: str, actor: Actor = Depends(_actor)) -> dict:
    return await service.ops_doc(get_db(), kind)


class OpsDraftBody(BaseModel):
    data: list[dict[str, Any]] = Field(..., max_length=500)


@router.put("/ops-data/{kind}/draft")
async def save_ops_draft(kind: str, body: OpsDraftBody, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "edit_ops_data")
    now = service.now_iso()
    doc = await service.update_ops_doc(db, kind, lambda d: rules.save_ops_draft(d, body.data, actor.name, now))
    return doc


@router.delete("/ops-data/{kind}/draft")
async def discard_ops_draft(kind: str, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "edit_ops_data")
    await service.update_ops_doc(db, kind, lambda d: {**d, "draft": None})
    return {"ok": True}


class OpsNoteBody(BaseModel):
    note: str = Field("", max_length=1000)


@router.post("/ops-data/{kind}/publish")
async def publish_ops_data(kind: str, body: OpsNoteBody, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "publish")
    now = service.now_iso()
    doc = await service.update_ops_doc(db, kind, lambda d: must(rules.publish_ops(kind, d, body.note, actor.name, now)))
    await service.audit(db, actor, "ops_data.publish", f"ops-data/{kind}", f"{_KIND_TITLE[kind]} v{doc['versions'][0]['number']}",
                        body.note.strip())
    await service.after_publish(db)
    return {"ok": True}


class OpsRollbackBody(BaseModel):
    toNumber: int = Field(..., ge=1)
    reason: str = Field("", max_length=1000)


@router.post("/ops-data/{kind}/rollback")
async def rollback_ops_data(kind: str, body: OpsRollbackBody, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "rollback")
    now = service.now_iso()
    await service.update_ops_doc(db, kind, lambda d: must(rules.rollback_ops(d, body.toNumber, body.reason, actor.name, now)))
    await service.audit(db, actor, "ops_data.rollback", f"ops-data/{kind}", f"{_KIND_TITLE[kind]} restores v{body.toNumber}",
                        body.reason.strip())
    await service.after_publish(db)
    return {"ok": True}


class ReasonBody(BaseModel):
    reason: str = Field("", max_length=1000)


@router.post("/ops-data/{kind}/revert")
async def revert_ops_data(kind: str, body: ReasonBody, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "rollback")
    now = service.now_iso()
    await service.update_ops_doc(db, kind, lambda d: must(rules.revert_ops_to_code(d, body.reason, actor.name, now)))
    await service.audit(db, actor, "ops_data.revert_to_code", f"ops-data/{kind}", _KIND_TITLE[kind], body.reason.strip())
    await service.after_publish(db)
    return {"ok": True}


# ---------------------------------------------------------------------------------------------------------------------
# Access list
# ---------------------------------------------------------------------------------------------------------------------

@router.get("/staff")
async def list_staff(actor: Actor = Depends(_actor)) -> list[dict]:
    return await service.list_staff(get_db())


class StaffBody(BaseModel):
    email: str = Field(..., max_length=200)
    name: str = Field("", max_length=120)
    role: str = Field(..., pattern="^(editor|publisher|admin)$")


@router.post("/staff")
async def add_staff(body: StaffBody, actor: Actor = Depends(_actor)) -> list[dict]:
    db = get_db()
    service.require(actor, "manage_access")
    staff = await service.list_staff(db)
    problem = rules.check_add_staff(staff, body.email, body.role)
    if problem:
        raise LibraryError("invalid", problem)
    mail = body.email.strip().lower()
    await db.prompt_library_staff.insert_one({"_id": mail, "id": uuid.uuid4().hex[:12], "email": mail,
                                              "name": body.name.strip() or mail.split("@")[0], "role": body.role,
                                              "addedAt": service.now_iso()})
    await service.audit(db, actor, "access.add", "access", mail, body.role)
    return await service.list_staff(db)


class RoleBody(BaseModel):
    role: str = Field(..., pattern="^(editor|publisher|admin)$")


@router.put("/staff/{member_id}")
async def change_staff_role(member_id: str, body: RoleBody, actor: Actor = Depends(_actor)) -> list[dict]:
    db = get_db()
    service.require(actor, "manage_access")
    staff = await service.list_staff(db)
    problem = rules.check_set_role(staff, member_id, body.role)
    if problem:
        raise LibraryError("invalid", problem)
    who = next(s for s in staff if s["id"] == member_id)
    await db.prompt_library_staff.update_one({"id": member_id}, {"$set": {"role": body.role}})
    await service.audit(db, actor, "access.role", "access", f"{who['name']} is now {body.role}")
    return await service.list_staff(db)


@router.delete("/staff/{member_id}")
async def remove_staff(member_id: str, actor: Actor = Depends(_actor)) -> list[dict]:
    db = get_db()
    service.require(actor, "manage_access")
    staff = await service.list_staff(db)
    problem = rules.check_remove_staff(staff, member_id)
    if problem:
        raise LibraryError("invalid", problem)
    who = next(s for s in staff if s["id"] == member_id)
    await db.prompt_library_staff.delete_one({"id": member_id})
    await service.audit(db, actor, "access.remove", "access", who["name"])
    return await service.list_staff(db)


# ---------------------------------------------------------------------------------------------------------------------
# Comments and notifications
# ---------------------------------------------------------------------------------------------------------------------

async def _comments(db, path: str) -> list[dict]:
    return [d async for d in db.prompt_comments.find({"path": path}, {"_id": 0, "path": 0}).sort("at", 1)]


@router.get("/comments")
async def list_comments(path: str = Query(..., max_length=300), actor: Actor = Depends(_actor)) -> list[dict]:
    return await _comments(get_db(), path)


class CommentBody(BaseModel):
    path: str = Field(..., max_length=300)
    text: str = Field("", max_length=4000)
    regionId: str | None = Field(None, max_length=100)


@router.post("/comments")
@limiter.limit("60/minute")
async def add_comment(request: Request, body: CommentBody, actor: Actor = Depends(_actor)) -> list[dict]:
    db = get_db()
    service.require(actor, "comment")
    await service.find(db, body.path)
    problem = rules.check_comment(body.text)
    if problem:
        raise LibraryError("invalid", problem)
    await db.prompt_comments.insert_one({"id": uuid.uuid4().hex[:12], "path": body.path, "by": actor.name, "byId": actor.id,
                                         "at": service.now_iso(), "text": body.text.strip(), "regionId": body.regionId,
                                         "resolvedBy": None, "resolvedAt": None})
    await service.audit(db, actor, "comment.add", body.path, body.text.strip()[:80])
    return await _comments(db, body.path)


class ResolveBody(BaseModel):
    path: str = Field(..., max_length=300)
    id: str = Field(..., max_length=64)
    resolved: bool


@router.post("/comments/resolve")
async def resolve_comment(body: ResolveBody, actor: Actor = Depends(_actor)) -> list[dict]:
    db = get_db()
    service.require(actor, "comment")
    update = {"resolvedBy": actor.name, "resolvedAt": service.now_iso()} if body.resolved else {"resolvedBy": None, "resolvedAt": None}
    result = await db.prompt_comments.update_one({"path": body.path, "id": body.id}, {"$set": update})
    if result.matched_count == 0:
        raise LibraryError("not_found", "That comment isn't there any more.")
    return await _comments(db, body.path)


@router.get("/notifications")
async def list_notifications(actor: Actor = Depends(_actor)) -> list[dict]:
    db = get_db()
    _, rows, states = await _index(db, with_states=True)
    requests = [{**r, "path": r["_id"]} async for r in db.prompt_requests.find({})]
    comments = [{"path": c["path"], "comment": c} async for c in db.prompt_comments.find({"resolvedAt": None}, {"_id": 0})]
    dismissed = set((await db.prompt_notice_dismissals.find_one({"_id": actor.id}) or {}).get("ids") or [])
    return rules.build_notifications(
        me=actor.id, role=actor.role,
        prompts=[{"path": r["path"], "flags": r["attention"], "changedAt": r["updatedAt"],
                  "codeAt": ((states.get(r["path"]) or {}).get("repoChange") or {}).get("at"),
                  "orphans": ",".join((states.get(r["path"]) or {}).get("orphaned") or [])} for r in rows],
        requests=requests, comments=comments, dismissed=dismissed,
    )


class DismissBody(BaseModel):
    ids: list[str] = Field(..., max_length=500)


@router.post("/notifications/dismiss")
async def dismiss_notifications(body: DismissBody, actor: Actor = Depends(_actor)) -> dict:
    # only the latest 500 are kept: older notices have long gone from the list anyway
    await get_db().prompt_notice_dismissals.update_one({"_id": actor.id}, {"$push": {"ids": {"$each": body.ids, "$slice": -500}}},
                                                       upsert=True)
    return {"ok": True}


# ---------------------------------------------------------------------------------------------------------------------
# Where published versions are used (admin only)
# ---------------------------------------------------------------------------------------------------------------------

@router.get("/switches")
async def get_switches(actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    switches = await service.get_switches(db)
    return {**switches, "available": [{"id": g, "label": label} for g, label in live.GROUPS]}


class SwitchBody(BaseModel):
    enabled: bool
    groups: list[str] = Field(default_factory=list, max_length=20)


@router.put("/switches")
async def set_switches(body: SwitchBody, actor: Actor = Depends(_actor)) -> dict:
    db = get_db()
    service.require(actor, "manage_rules")
    before = await service.get_switches(db)
    after = await service.set_switches(db, body.enabled, body.groups)
    await service.audit(db, actor, "switches.change", "switches",
                        f"{'On' if after['enabled'] else 'Off'}; groups: {', '.join(after['groups']) or 'none'}",
                        f"Was {'on' if before['enabled'] else 'off'}; groups: {', '.join(before['groups']) or 'none'}")
    return {**after, "available": [{"id": g, "label": label} for g, label in live.GROUPS]}


# ---------------------------------------------------------------------------------------------------------------------
# For members: which tones the picker may show
# ---------------------------------------------------------------------------------------------------------------------

@lists_router.get("/tones")
async def shown_tones(_: dict = Depends(get_current_user)) -> dict:
    return {"shown": ops_lists.shown_tones()}

