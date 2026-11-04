"""Two-step sign-in with an authenticator app, and the backup codes that stand in for it.

After the emailed code, a person who turned this on also gives the six digits their authenticator app shows. Each code works once
(a used time step is remembered, so a code seen over a shoulder can't be used again), and ten backup codes cover a lost phone: each
works once and is kept only as a hash. The secret is stored encrypted. Wrong codes are counted, so six digits can't be guessed."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Optional

import pyotp
from fastapi import HTTPException

from app.db.mongo import users
from app.db.redis import get_redis
from app.pipelines.publish.token_store import decrypt_token, encrypt_token

ISSUER = "Ekavo"
STEP_SECONDS = 30
BACKUP_COUNT = 10
CHALLENGE_SECONDS = 300
CHALLENGE_TRIES = 5
SETTINGS_TRIES = 8
SETTINGS_WINDOW_SECONDS = 900
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

WRONG = "That code didn't work. Check the code in your app, or use a backup code."


def _normalise(code: str) -> str:
    return "".join(ch for ch in (code or "").lower() if ch.isalnum())


def _hash(code: str) -> str:
    return hashlib.sha256(_normalise(code).encode()).hexdigest()


def _new_backup_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _matching_step(secret: str, code: str, after: int) -> Optional[int]:
    """The time step this six-digit code belongs to (this one or the one either side, for clock drift), if it is newer than the last one used."""
    code = _normalise(code)
    if len(code) != 6 or not code.isdigit():
        return None
    now_step = int(time.time() // STEP_SECONDS)
    totp = pyotp.TOTP(secret, interval=STEP_SECONDS)
    for step in (now_step - 1, now_step, now_step + 1):
        if step > after and hmac.compare_digest(totp.at(step * STEP_SECONDS), code):
            return step
    return None


async def _throttle(key: str, limit: int, ttl: int) -> None:
    redis = await get_redis()
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, ttl)
    if count > limit:
        raise HTTPException(status_code=429, detail="Too many tries. Wait a few minutes and try again.")


def is_on(user: dict[str, Any]) -> bool:
    return bool((user.get("two_step") or {}).get("enabled"))


def status(user: dict[str, Any]) -> dict[str, Any]:
    ts = user.get("two_step") or {}
    return {"enabled": bool(ts.get("enabled")), "backup_left": len(ts.get("backup") or []), "enabled_at": ts.get("enabled_at")}


async def start_setup(user: dict[str, Any]) -> dict[str, str]:
    if is_on(user):
        raise HTTPException(status_code=409, detail="Two-step sign-in is already on.")
    secret = pyotp.random_base32()
    await users.update_one({"id": user["id"]}, {"$set": {"two_step.pending": encrypt_token(secret)}})
    account = user.get("email") or user.get("username") or user["id"]
    return {"secret": secret, "uri": pyotp.TOTP(secret, interval=STEP_SECONDS).provisioning_uri(name=account, issuer_name=ISSUER)}


async def enable(user: dict[str, Any], code: str) -> list[str]:
    """Turn it on once the person proves their app shows the right code. Returns the backup codes, which are never shown again."""
    await _throttle(f"two_step_settings:{user['id']}", SETTINGS_TRIES, SETTINGS_WINDOW_SECONDS)
    pending = (user.get("two_step") or {}).get("pending")
    if is_on(user):
        raise HTTPException(status_code=409, detail="Two-step sign-in is already on.")
    if not pending:
        raise HTTPException(status_code=400, detail="Start again: ask for a new setup code.")
    secret = decrypt_token(pending)
    step = _matching_step(secret, code, 0)
    if step is None:
        raise HTTPException(status_code=400, detail="That code didn't match. Check the code in your app and try again.")
    backups = [_new_backup_code() for _ in range(BACKUP_COUNT)]
    await users.update_one({"id": user["id"]}, {
        "$set": {"two_step": {"enabled": True, "secret": encrypt_token(secret), "backup": [_hash(c) for c in backups],
                              "last_step": step, "enabled_at": datetime.now(timezone.utc)}},
    })
    return backups


async def check(user: dict[str, Any], code: str) -> bool:
    """A code from the app, or an unused backup code. Either is used up by being accepted here."""
    ts = user.get("two_step") or {}
    if not ts.get("enabled"):
        return True
    step = _matching_step(decrypt_token(ts["secret"]), code, int(ts.get("last_step") or 0))
    if step is not None:
        # one write decides it: if another request used a newer code at the same moment, this one is refused
        res = await users.update_one({"id": user["id"], "two_step.last_step": {"$lt": step}}, {"$set": {"two_step.last_step": step}})
        return res.modified_count == 1
    digest = _hash(code)
    if len(_normalise(code)) == 8:
        res = await users.update_one({"id": user["id"], "two_step.backup": digest}, {"$pull": {"two_step.backup": digest}})
        return res.modified_count == 1
    return False


async def disable(user: dict[str, Any], code: str) -> None:
    await _throttle(f"two_step_settings:{user['id']}", SETTINGS_TRIES, SETTINGS_WINDOW_SECONDS)
    if not is_on(user):
        raise HTTPException(status_code=409, detail="Two-step sign-in is already off.")
    if not await check(user, code):
        raise HTTPException(status_code=400, detail=WRONG)
    await users.update_one({"id": user["id"]}, {"$unset": {"two_step": ""}})


async def new_backup_codes(user: dict[str, Any], code: str) -> list[str]:
    await _throttle(f"two_step_settings:{user['id']}", SETTINGS_TRIES, SETTINGS_WINDOW_SECONDS)
    if not is_on(user):
        raise HTTPException(status_code=409, detail="Turn on two-step sign-in first.")
    if not await check(user, code):
        raise HTTPException(status_code=400, detail=WRONG)
    backups = [_new_backup_code() for _ in range(BACKUP_COUNT)]
    await users.update_one({"id": user["id"]}, {"$set": {"two_step.backup": [_hash(c) for c in backups]}})
    return backups


async def open_challenge(user_id: str) -> str:
    """After the emailed code: a short-lived ticket the person trades, with their app's code, for a sign-in."""
    token = secrets.token_urlsafe(24)
    redis = await get_redis()
    await redis.set(f"two_step:{token}", user_id, ex=CHALLENGE_SECONDS)
    return token


async def finish_challenge(token: str, code: str) -> dict[str, Any]:
    """The user document for a ticket whose code is right. Five wrong codes end the ticket, so the person signs in from the start."""
    redis = await get_redis()
    raw = await redis.get(f"two_step:{token}")
    if not raw:
        raise HTTPException(status_code=401, detail="That took too long. Sign in again from the start.")
    user_id = raw.decode() if isinstance(raw, bytes) else str(raw)
    tries = await redis.incr(f"two_step_tries:{token}")
    if tries == 1:
        await redis.expire(f"two_step_tries:{token}", CHALLENGE_SECONDS)
    if tries > CHALLENGE_TRIES:
        await redis.delete(f"two_step:{token}")
        raise HTTPException(status_code=401, detail="Too many wrong codes. Sign in again from the start.")
    user = await users.find_one({"id": user_id})
    if not user or not await check(user, code):
        raise HTTPException(status_code=401, detail=WRONG)
    await redis.delete(f"two_step:{token}")
    return user
