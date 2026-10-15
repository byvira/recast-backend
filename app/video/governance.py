"""Workspace-level controls for video that are about trust rather than editing:

* access        staff can switch video off for one workspace (a closed-group rollout, or a workspace that has to be held). On by default.
* privacy       a workspace can keep its audio away from outside speech services. Writing out words sends audio to a service, so with
                this on it is refused with a plain message. There is no self-hosted speech option yet, so the switch is off or on.
* consent       a record that the person whose voice or face is used agreed, what they agreed to, and when they withdrew it. Cloned
                voices and faces (a later batch) must have an active record; the check lives here so every feature asks the same question.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from app.db.mongo import get_db

CONSENT_KINDS = ("voice", "face")
ACCESS_OFF = "Video is not switched on for this workspace yet. Ask the Ekavo team to turn it on."
PRIVACY_OFF = ("Writing out words is switched off for this workspace, because its audio is set to stay away from outside services. "
               "An owner can change this in Settings.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ws():
    return get_db()["workspaces"]


async def video_enabled(workspace_id: str) -> bool:
    doc = await _ws().find_one({"id": workspace_id}, {"video_access": 1})
    return (doc or {}).get("video_access", {}).get("enabled", True) is not False


async def set_access(workspace_id: str, enabled: bool, staff_id: str, note: str = "") -> bool:
    res = await _ws().update_one({"id": workspace_id}, {"$set": {"video_access": {"enabled": bool(enabled), "by": staff_id, "note": note[:200], "at": _now()}}})
    return bool(res.matched_count)


async def transcription_allowed(workspace_id: str) -> bool:
    doc = await _ws().find_one({"id": workspace_id}, {"video_privacy": 1})
    return (doc or {}).get("video_privacy", {}).get("keep_audio_private", False) is not True


async def privacy(workspace_id: str) -> dict[str, Any]:
    doc = await _ws().find_one({"id": workspace_id}, {"video_privacy": 1}) or {}
    return {"keep_audio_private": bool((doc.get("video_privacy") or {}).get("keep_audio_private", False))}


async def set_privacy(workspace_id: str, keep_audio_private: bool, user_id: str) -> dict[str, Any]:
    await _ws().update_one({"id": workspace_id}, {"$set": {"video_privacy": {"keep_audio_private": bool(keep_audio_private), "by": user_id, "at": _now()}}})
    return await privacy(workspace_id)


# ── consent ──────────────────────────────────────────────────────────────────────

class ConsentError(ValueError):
    """A refusal with a plain message."""


def _consents():
    return get_db()["video_consents"]


def _iso(v: Any) -> Optional[str]:
    return v.replace(tzinfo=v.tzinfo or timezone.utc).isoformat().replace("+00:00", "Z") if isinstance(v, datetime) else None


def _public(c: dict) -> dict[str, Any]:
    return {"id": c["id"], "kind": c["kind"], "person": c["person"], "scope": c["scope"], "evidence": c["evidence"], "given_on": c["given_on"],
            "recorded_by": c["recorded_by"], "recorded_at": _iso(c.get("recorded_at")), "withdrawn": bool(c.get("withdrawn_at")), "withdrawn_at": _iso(c.get("withdrawn_at"))}


async def record_consent(workspace_id: str, user_id: str, *, kind: Literal["voice", "face"], person: str, scope: str, evidence: str, given_on: str) -> dict[str, Any]:
    """Record that `person` agreed. `scope` says what they agreed to, `evidence` says how it was given (for example a signed form kept in
    the team's own files), `given_on` is the date they agreed."""
    if kind not in CONSENT_KINDS:
        raise ConsentError("Consent is recorded for a voice or a face.")
    person, scope, evidence = person.strip(), scope.strip(), evidence.strip()
    if not person:
        raise ConsentError("Say whose voice or face this is.")
    if len(scope) < 10:
        raise ConsentError("Say what they agreed to, for example making videos in their voice for the company's own channels.")
    if len(evidence) < 5:
        raise ConsentError("Say how they agreed, for example a signed form kept in your files, with its date.")
    try:
        datetime.strptime(given_on, "%Y-%m-%d")
    except ValueError:
        raise ConsentError("Give the date they agreed as year, month and day.")
    doc = {"id": uuid4().hex, "workspace_id": workspace_id, "kind": kind, "person": person[:120], "scope": scope[:500], "evidence": evidence[:500], "given_on": given_on,
           "recorded_by": user_id, "recorded_at": _now(), "withdrawn_at": None}
    await _consents().insert_one(dict(doc))
    return _public(doc)


async def list_consents(workspace_id: str) -> list[dict[str, Any]]:
    return [_public(c) for c in await _consents().find({"workspace_id": workspace_id}).sort("recorded_at", -1).to_list(length=200)]


async def withdraw(workspace_id: str, consent_id: str, user_id: str) -> bool:
    res = await _consents().update_one({"id": consent_id, "workspace_id": workspace_id, "withdrawn_at": None}, {"$set": {"withdrawn_at": _now(), "withdrawn_by": user_id}})
    return bool(res.modified_count)


async def has_consent(workspace_id: str, kind: str, person: str) -> bool:
    """True when there is an active record for this person. Withdrawn consent no longer counts."""
    return bool(await _consents().find_one({"workspace_id": workspace_id, "kind": kind, "person": {"$regex": f"^{__import__('re').escape(person.strip())}$", "$options": "i"}, "withdrawn_at": None}))
