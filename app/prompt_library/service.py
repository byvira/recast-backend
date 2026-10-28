"""The Ops prompt library's storage: the prompt catalog, each prompt's state, people, the activity log and the switches.

The code owns every prompt file. A prompt's state (its versions, draft, checks and code-change notices) is one record in
`prompt_state`, written with a revision check so two people saving at once can never overwrite each other. New prompts
made in Ops live in `prompt_proposed` until a developer adds a file at the same path.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from pymongo.errors import DuplicateKeyError

from app.prompt_library import live, ops_lists, rules
from app.prompt_library.regions import Region, compose, fingerprint, parse_regions, strip_markers

PROMPTS_DIR = live.PROMPTS_DIR


class LibraryError(Exception):
    """A refusal with a plain message. `code` is conflict, forbidden, invalid or not_found (as the screens expect)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class Actor:
    id: str
    name: str
    email: str
    role: str


# ---------------------------------------------------------------------------------------------------------------------
# The catalog: prompt files plus prompts proposed in Ops
# ---------------------------------------------------------------------------------------------------------------------

@dataclass
class Prompt:
    path: str
    folder: str
    name: str
    klass: str
    reason: str
    safety_sensitive: bool
    text: str
    used_by: list[str]
    protected: list[str]
    receives: list[dict]
    receives_more: bool
    proposed: bool

    @property
    def regions(self) -> list[Region]:
        return parse_regions(self.text)

    @property
    def lines(self) -> int:
        return len(strip_markers(self.text).splitlines())

    @property
    def repo_hash(self) -> str:
        """Changes when the text or the sections change (a renamed section, or a locked one made editable)."""
        return fingerprint(self.text)

    @property
    def extra_slots(self) -> list[str]:
        return [r["name"] for r in self.receives]


_file_cache: dict[str, tuple[float, str]] = {}


def _file_text(path: str) -> str | None:
    file = PROMPTS_DIR / f"{path}.jinja"
    try:
        mtime = os.path.getmtime(file)
    except OSError:
        return None
    cached = _file_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    text = file.read_text(encoding="utf-8").replace("\r\n", "\n")
    _file_cache[path] = (mtime, text)
    return text


def file_paths() -> list[str]:
    return sorted(p.relative_to(PROMPTS_DIR).as_posix()[:-6] for p in PROMPTS_DIR.rglob("*.jinja"))


def _split(path: str) -> tuple[str, str]:
    return (path.rsplit("/", 1)[0], path.rsplit("/", 1)[1]) if "/" in path else ("", path)


def valid_path(path: str) -> bool:
    """A prompt path is folder and file names of lower case letters, numbers and underscores, nothing else."""
    parts = path.split("/") if isinstance(path, str) else []
    return 0 < len(parts) <= 8 and all(rules.PATH_SEGMENT.match(p) or (p == "_test_fixture") for p in parts)


def file_prompt(path: str) -> Prompt | None:
    if not valid_path(path):
        return None
    text = _file_text(path)
    if text is None:
        return None
    info = live.manifest().get(path) or {}
    folder, name = _split(path)
    return Prompt(
        path=path, folder=folder, name=name, klass=info.get("klass", "dev"), reason=info.get("reason", ""),
        safety_sensitive=bool(info.get("safetySensitive")), text=text, used_by=list(info.get("usedBy") or []),
        protected=list(info.get("protectedLiterals") or []), receives=list(info.get("receives") or []),
        receives_more=bool(info.get("receivesMore")), proposed=False,
    )


def _proposed_prompt(doc: dict) -> Prompt:
    folder, name = _split(doc["_id"])
    return Prompt(path=doc["_id"], folder=folder, name=name, klass="ops", reason=doc.get("reason", ""), safety_sensitive=False,
                  text=doc["text"], used_by=[], protected=[], receives=[], receives_more=False, proposed=True)


async def catalog(db) -> dict[str, Prompt]:
    out = {p: fp for p in file_paths() if (fp := file_prompt(p)) is not None}
    async for doc in db.prompt_proposed.find({}):
        if doc["_id"] not in out:
            out[doc["_id"]] = _proposed_prompt(doc)
    return out


async def find(db, path: str) -> Prompt:
    if not valid_path(path):
        raise LibraryError("not_found", "This prompt file doesn't exist.")
    prompt = file_prompt(path)
    if prompt is None:
        doc = await db.prompt_proposed.find_one({"_id": path})
        if doc is None:
            raise LibraryError("not_found", "This prompt file doesn't exist.")
        prompt = _proposed_prompt(doc)
    return prompt


async def all_folders(db, prompts: dict[str, Prompt] | None = None) -> set[str]:
    prompts = prompts if prompts is not None else await catalog(db)
    out: set[str] = set()
    folders = [p.folder for p in prompts.values()] + [d["_id"] async for d in db.prompt_folders.find({}, {"_id": 1})]
    for f in folders:
        parts = [x for x in f.split("/") if x]
        for i in range(len(parts)):
            out.add("/".join(parts[: i + 1]))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Prompt state (one record per prompt, written with a revision check)
# ---------------------------------------------------------------------------------------------------------------------

_STATE_KEYS = ("versions", "draft", "defaults", "repoHash", "repoChange", "orphaned", "checks", "draftSeq")


def _state_of(doc: dict | None, prompt: Prompt) -> dict:
    if not doc:
        return rules.empty_state(prompt.regions, prompt.repo_hash)
    return {k: doc.get(k) for k in _STATE_KEYS} | {"versions": doc.get("versions") or [], "orphaned": doc.get("orphaned") or [],
                                                     "defaults": doc.get("defaults") or {}}


async def get_state(db, prompt: Prompt) -> dict:
    return _state_of(await db.prompt_state.find_one({"_id": prompt.path}), prompt)


async def all_states(db) -> dict[str, dict]:
    return {d["_id"]: d async for d in db.prompt_state.find({})}


async def update_state(db, prompt: Prompt, change: Callable[[dict], dict]) -> dict:
    """Applies `change` to the stored state and saves it, retrying if someone else saved in between."""
    for _ in range(5):
        doc = await db.prompt_state.find_one({"_id": prompt.path})
        rev = (doc or {}).get("rev", 0)
        new = change(_state_of(doc, prompt))
        body = {k: new.get(k) for k in _STATE_KEYS} | {"rev": rev + 1, "updatedAt": now_iso()}
        if doc is None:
            try:
                await db.prompt_state.insert_one({"_id": prompt.path, **body})
                return new
            except DuplicateKeyError:
                continue
        result = await db.prompt_state.update_one({"_id": prompt.path, "rev": rev}, {"$set": body})
        if result.modified_count == 1:
            return new
    raise LibraryError("conflict", "Someone else changed this at the same time. Reload and try again.")


def must(result: rules.Result, code: str = "invalid") -> Any:
    passed, value = result
    if not passed:
        raise LibraryError(code, value)
    return value


# ---------------------------------------------------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------------------------------------------------

def _display_name(user: dict) -> str:
    return str(user.get("name") or user.get("username") or user.get("email") or "A teammate")


async def ensure_staff_seeded(db) -> None:
    """The access list starts with every master admin as an admin, so it is never empty."""
    if await db.prompt_library_staff.count_documents({}, limit=1):
        return
    at = now_iso()
    async for u in db.users.find({"is_master_admin": True}, {"_id": 1, "id": 1, "email": 1, "name": 1}):
        email = str(u.get("email") or "").lower()
        if not email:
            continue
        try:
            await db.prompt_library_staff.insert_one({"_id": email, "id": uuid.uuid4().hex[:12], "email": email,
                                                       "name": _display_name(u), "role": "admin", "addedAt": at})
        except DuplicateKeyError:
            pass


async def actor_for(db, user: dict) -> Actor:
    """Who is acting, and their library role. Master admins are always admin. Platform staff who are not on the access
    list are editors: they can draft and test, and nothing they do goes live without a publisher."""
    email = str(user.get("email") or "").lower()
    if user.get("is_master_admin"):
        role = "admin"
    else:
        member = await db.prompt_library_staff.find_one({"_id": email}) if email else None
        role = member["role"] if member else "editor"
    return Actor(id=str(user.get("id") or user.get("_id") or email), name=_display_name(user), email=email, role=role)


def require(actor: Actor, action: str) -> None:
    if not rules.can(actor.role, action):
        raise LibraryError("forbidden", rules.FORBIDDEN)


async def list_staff(db) -> list[dict]:
    await ensure_staff_seeded(db)
    rows = [d async for d in db.prompt_library_staff.find({}).sort("addedAt", 1)]
    return [{"id": d["id"], "name": d["name"], "email": d["email"], "role": d["role"], "addedAt": d["addedAt"]} for d in rows]


# ---------------------------------------------------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------------------------------------------------

async def audit(db, actor: Actor | str, action: str, subject: str, detail: str, reason: str | None = None) -> None:
    name, actor_id = (actor.name, actor.id) if isinstance(actor, Actor) else (actor, None)
    await db.prompt_library_audit.insert_one({
        "id": uuid.uuid4().hex, "at": now_iso(), "actor": name, "actorId": actor_id, "action": action,
        "subject": subject, "detail": detail, "reason": reason,
    })


async def list_audit(db, path: str | None = None, limit: int = 1000) -> list[dict]:
    query = {"subject": path} if path else {}
    cursor = db.prompt_library_audit.find(query, {"_id": 0, "actorId": 0}).sort("at", -1).limit(limit)
    return [d async for d in cursor]


# ---------------------------------------------------------------------------------------------------------------------
# Switches (where published versions are used)
# ---------------------------------------------------------------------------------------------------------------------

async def get_switches(db) -> dict:
    doc = await db.prompt_library_settings.find_one({"_id": "switches"}) or {}
    return {"enabled": bool(doc.get("enabled")), "groups": [g for g in doc.get("groups") or [] if g in live.GROUP_IDS]}


async def set_switches(db, enabled: bool, groups: list[str]) -> dict:
    clean = [g for g in live.GROUP_IDS if g in set(groups)]
    await db.prompt_library_settings.update_one({"_id": "switches"},
                                                {"$set": {"enabled": bool(enabled), "groups": clean}, "$inc": {"counter": 1}},
                                                upsert=True)
    await live.refresh(db)
    return {"enabled": bool(enabled), "groups": clean}


async def after_publish(db) -> None:
    """Makes a publish take effect on this server at once; the others pick it up on their next refresh."""
    await db.prompt_library_settings.update_one({"_id": "switches"}, {"$inc": {"counter": 1}}, upsert=True)
    await live.refresh(db)


# ---------------------------------------------------------------------------------------------------------------------
# Ops data documents
# ---------------------------------------------------------------------------------------------------------------------

async def ops_doc(db, kind: str) -> dict:
    if kind not in ops_lists.KINDS:
        raise LibraryError("not_found", "That list doesn't exist.")
    doc = await db.prompt_ops_data.find_one({"_id": kind}) or {}
    return {"codeDefault": ops_lists.code_defaults(kind), "versions": doc.get("versions") or [], "draft": doc.get("draft")}


async def update_ops_doc(db, kind: str, change: Callable[[dict], dict]) -> dict:
    """Applies `change` to an Ops list's record and saves it, retrying if someone else saved in between."""
    for _ in range(5):
        stored = await db.prompt_ops_data.find_one({"_id": kind})
        rev = (stored or {}).get("rev", 0)
        new = change(await ops_doc(db, kind))
        body = {"versions": new["versions"], "draft": new["draft"], "rev": rev + 1}
        if stored is None:
            try:
                await db.prompt_ops_data.insert_one({"_id": kind, **body})
                return new
            except DuplicateKeyError:
                continue
        if (await db.prompt_ops_data.update_one({"_id": kind, "rev": rev}, {"$set": body})).modified_count == 1:
            return new
    raise LibraryError("conflict", "Someone else changed this list at the same time. Reload and try again.")

