"""Sign-in with OAuth for assistants that connect by address, such as Claude (claude.ai, Desktop, mobile) and Claude Code.

This is a thin front over the sessions in `sessions.py`; it adds no second kind of sign-in. The person still checks what is asking, picks a workspace and
what it may do, and says yes in the app. What changes is how the assistant asks and receives the answer, following the MCP authorization rules:

  * `register`  an assistant introduces itself and gets a client id (RFC 7591). It is a public client: it has no secret, and every sign-in uses PKCE (S256).
  * `begin`     the assistant sends the person here; the request is checked (client, exact redirect address, PKCE, the resource) and the person is sent to the app.
  * `approve`   the person says yes. A one-use code is made for the assistant's redirect address.
  * `exchange`  the assistant trades the code and its PKCE proof for an access token and a refresh token.
  * `refresh`   a new access token and a new refresh token. The old refresh token stops working; using it again closes the whole connection.
  * `revoke`    the assistant signs itself out.

The access token is the same kind of token a command line sign-in gets: one session row per connection, acting as that person with their role, capped to the
scopes they chose, shown in Settings, Connections, revocable there, counted by the plan's sign-in limit and stopped by the "assistants" switch in Ops. The row
is kept for the whole connection (90 days). The access token on it is short (one hour) and is replaced in place at each refresh, so the list never fills up."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from uuid import uuid4

from app.core.config import settings
from app.db.mongo import get_db
from app.tools import sessions
from app.tools.contract import Scope
from app.video import api_keys

SCOPES = ("read", "create", "publish", "approve")
CLIENT_DAYS = 60                                        # an introduction nobody has used for this long is forgotten
MAX_REDIRECTS = 5
MAX_REGISTRATIONS_A_DAY = 500                           # the whole app, so a flood of introductions cannot fill the database
REQUEST_LIFETIME = timedelta(minutes=10)                # the time the person has to answer in the app
CODE_LIFETIME = timedelta(minutes=5)
ACCESS_LIFETIME = timedelta(hours=1)
REFRESH_GRACE_SECONDS = 60                              # a repeat of the refresh token just used (two calls at once) is refused but does not close the connection
LOOPBACK = ("localhost", "127.0.0.1", "[::1]", "::1")


class OAuthError(ValueError):
    """A refusal in OAuth's own words: `error` is one of the codes the standard names, `description` is plain."""

    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error, self.description, self.status = error, description, status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clients():
    return get_db()["oauth_clients"]


def _requests():
    return get_db()["oauth_requests"]


def _codes():
    return get_db()["oauth_codes"]


async def create_indexes() -> None:
    try:
        await _clients().create_index("client_id", unique=True)
        await _clients().create_index("expires_at", expireAfterSeconds=0)
        await _clients().create_index("created_at")
        await _requests().create_index("id", unique=True)
        await _requests().create_index("expires_at", expireAfterSeconds=3600)
        await _codes().create_index("code_hash", unique=True)
        await _codes().create_index("expires_at", expireAfterSeconds=3600)
        await api_keys._keys().create_index("oauth.refresh_hash", sparse=True)
        await api_keys._keys().create_index("oauth.prev_refresh_hash", sparse=True)
    except Exception:  # noqa: BLE001 - optional: the app starts without them
        pass


# ── the addresses this server answers at ─────────────────────────────────────

def api_base() -> str:
    return settings.API_PUBLIC_URL.rstrip("/")


def resource_url() -> str:
    """The canonical address of the MCP server: the one a token is made for."""
    return f"{api_base()}/api/public/v1/mcp"


def resource_metadata_url() -> str:
    return f"{api_base()}/.well-known/oauth-protected-resource/api/public/v1/mcp"


def protected_resource_metadata() -> dict[str, Any]:
    return {
        "resource": resource_url(), "authorization_servers": [api_base()], "scopes_supported": list(SCOPES), "bearer_methods_supported": ["header"],
        "resource_name": "Ekavo", "resource_documentation": f"{settings.FRONTEND_URL.rstrip('/')}/developers",
    }


def authorization_server_metadata() -> dict[str, Any]:
    base = api_base()
    return {
        "issuer": base, "authorization_endpoint": f"{base}/oauth/authorize", "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register", "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"], "revocation_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"], "scopes_supported": list(SCOPES), "authorization_response_iss_parameter_supported": True,
        "service_documentation": f"{settings.FRONTEND_URL.rstrip('/')}/developers",
    }


# ── redirect addresses ────────────────────────────────────────────────────────

def _is_loopback(host: str) -> bool:
    return (host or "").lower() in LOOPBACK


def valid_redirect(uri: str) -> bool:
    """https anywhere, or http only to this computer (a command line tool listens there). No fragment, no name or password in the address."""
    if not isinstance(uri, str) or not uri or len(uri) > 300:
        return False
    try:
        parts = urlparse(uri)
        host = parts.hostname or ""
    except ValueError:
        return False
    if parts.fragment or parts.username or parts.password or not host:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and _is_loopback(host))


def redirect_allowed(registered: Iterable[str], given: str) -> bool:
    """Exactly one of the registered addresses. A tool on this computer picks a different port each time, so for those the port is ignored."""
    registered = list(registered)
    if given in registered:
        return True
    try:
        g = urlparse(given)
    except ValueError:
        return False
    if g.scheme != "http" or not _is_loopback(g.hostname or ""):
        return False
    for r in registered:
        try:
            p = urlparse(r)
        except ValueError:
            continue
        if p.scheme == "http" and (p.hostname or "").lower() == (g.hostname or "").lower() and p.path == g.path and p.query == g.query:
            return True
    return False


def _with_query(uri: str, extra: dict[str, str]) -> str:
    p = urlparse(uri)
    query = parse_qsl(p.query, keep_blank_values=True) + [(k, v) for k, v in extra.items() if v is not None]
    return urlunparse(p._replace(query=urlencode(query)))


# ── registration ──────────────────────────────────────────────────────────────

def _clean_name(value: Any) -> str:
    text = re.sub(r"[\x00-\x1f<>]", "", str(value or "")).strip()
    return " ".join(text.split())[:60] or "Assistant app"


async def register(body: dict[str, Any], *, address: str) -> dict[str, Any]:
    """An assistant introduces itself (RFC 7591). Public clients only; the redirect addresses are checked now and again, exactly, at every sign-in."""
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > MAX_REDIRECTS or not all(valid_redirect(u) for u in uris):
        raise OAuthError("invalid_redirect_uri", "Send one to five redirect addresses. They must be https, or http to this computer, with no fragment.")
    method = body.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise OAuthError("invalid_client_metadata", "Only public clients are supported: token_endpoint_auth_method must be none.")
    grants = body.get("grant_types") or ["authorization_code"]
    if not isinstance(grants, list) or not set(grants) <= {"authorization_code", "refresh_token"}:
        raise OAuthError("invalid_client_metadata", "grant_types may only be authorization_code and refresh_token.")
    since = _now() - timedelta(days=1)
    if await _clients().count_documents({"created_at": {"$gte": since}}) >= MAX_REGISTRATIONS_A_DAY:
        raise OAuthError("temporarily_unavailable", "Too many new apps were introduced today. Try again tomorrow.", 503)
    now = _now()
    client_id = "ekc_" + secrets.token_urlsafe(18)
    doc = {"client_id": client_id, "client_name": _clean_name(body.get("client_name")), "redirect_uris": list(dict.fromkeys(uris)), "created_at": now,
           "expires_at": now + timedelta(days=CLIENT_DAYS), "from": _hash(address)[:16]}
    await _clients().insert_one(dict(doc))
    return {"client_id": client_id, "client_id_issued_at": int(now.timestamp()), "client_name": doc["client_name"], "redirect_uris": doc["redirect_uris"],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": "none"}


async def _client(client_id: str) -> Optional[dict[str, Any]]:
    if not client_id or len(client_id) > 100:
        return None
    doc = await _clients().find_one({"client_id": client_id})
    if doc:
        await _clients().update_one({"client_id": client_id}, {"$set": {"expires_at": _now() + timedelta(days=CLIENT_DAYS)}})
    return doc


# ── the person's side of the sign-in ──────────────────────────────────────────

def _scopes_from(raw: str) -> list[str]:
    wanted = [s for s in (raw or "").replace(",", " ").split() if s in SCOPES]
    return list(dict.fromkeys(wanted)) or ["read"]


async def begin(params: dict[str, str]) -> str:
    """Check what the assistant sent and return the address in the app where the person answers.

    Raises `OAuthError` with `redirect=None` when the client or redirect address is wrong: nothing may be sent to an address that was not registered.
    Every other fault is sent back to the assistant's own address (see `fault_redirect`)."""
    client = await _client(params.get("client_id", ""))
    if client is None:
        raise OAuthError("invalid_client", "This app is not known. It has to introduce itself first.")
    redirect = params.get("redirect_uri") or (client["redirect_uris"][0] if len(client["redirect_uris"]) == 1 else "")
    if not redirect or not redirect_allowed(client["redirect_uris"], redirect):
        raise OAuthError("invalid_request", "That redirect address is not one this app registered.")
    state = params.get("state", "")[:500]
    fail = lambda error, text: _Redirect(redirect, state, error, text)  # noqa: E731
    if params.get("response_type") != "code":
        raise fail("unsupported_response_type", "Only response_type=code is supported.")
    challenge = params.get("code_challenge", "")
    if not challenge or params.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
        raise fail("invalid_request", "A PKCE code_challenge with code_challenge_method=S256 is required.")
    resource = params.get("resource", "")
    if resource and resource.rstrip("/") != resource_url():
        raise fail("invalid_target", "That resource is not this server.")
    rid = secrets.token_urlsafe(24)
    now = _now()
    await _requests().insert_one({"id": rid, "client_id": client["client_id"], "redirect_uri": redirect, "state": state, "code_challenge": challenge,
                                  "scopes": _scopes_from(params.get("scope", "")), "created_at": now, "expires_at": now + REQUEST_LIFETIME, "status": "pending"})
    return f"{settings.FRONTEND_URL.rstrip('/')}/oauth/authorize?{urlencode({'request': rid})}"


class _Redirect(OAuthError):
    """A fault to hand back to the assistant at its own registered redirect address."""

    def __init__(self, redirect: str, state: str, error: str, description: str):
        super().__init__(error, description)
        self.redirect, self.state = redirect, state

    def url(self) -> str:
        return _with_query(self.redirect, {"error": self.error, "error_description": self.description, "state": self.state, "iss": api_base()})


async def _open_request(request_id: str) -> dict[str, Any]:
    doc = await _requests().find_one({"id": request_id or "", "status": "pending"})
    if doc is None or (_aware(doc.get("expires_at")) or _now()) <= _now():
        raise sessions.SessionError("code_unknown", "This sign-in request has run out. Go back to the assistant and connect again.", 404)
    return doc


async def lookup(request_id: str) -> dict[str, Any]:
    """What is asking, for the person to check before they say yes."""
    doc = await _open_request(request_id)
    client = await _clients().find_one({"client_id": doc["client_id"]}) or {}
    host = urlparse(doc["redirect_uri"]).hostname or ""
    return {"client_name": client.get("client_name") or "Assistant app", "redirect_host": host, "on_this_computer": _is_loopback(host), "scopes": doc["scopes"]}


async def approve(request_id: str, user_id: str, workspace_id: str, scopes: Iterable[str], *, allow_approve: bool = False) -> str:
    """The person says yes. Returns the assistant's redirect address carrying a one-use code."""
    doc = await _open_request(request_id)
    chosen = await sessions._allowed_scopes(workspace_id, user_id, scopes, allow_approve)
    await sessions.ensure_room(workspace_id)
    claimed = await _requests().update_one({"id": doc["id"], "status": "pending"}, {"$set": {"status": "approved"}})
    if not claimed.modified_count:
        raise sessions.SessionError("code_unknown", "That sign-in was already answered.", 409)
    client = await _clients().find_one({"client_id": doc["client_id"]}) or {}
    code = secrets.token_urlsafe(32)
    now = _now()
    await _codes().insert_one({
        "code_hash": _hash(code), "client_id": doc["client_id"], "client_name": client.get("client_name") or "Assistant app", "redirect_uri": doc["redirect_uri"],
        "code_challenge": doc["code_challenge"], "workspace_id": workspace_id, "user_id": user_id, "scopes": chosen, "created_at": now,
        "expires_at": now + CODE_LIFETIME, "used": False,
    })
    return _with_query(doc["redirect_uri"], {"code": code, "state": doc["state"], "iss": api_base()})


async def deny(request_id: str) -> str:
    doc = await _open_request(request_id)
    await _requests().update_one({"id": doc["id"], "status": "pending"}, {"$set": {"status": "denied"}})
    return _Redirect(doc["redirect_uri"], doc["state"], "access_denied", "The person said no, so nothing was connected.").url()


# ── tokens ────────────────────────────────────────────────────────────────────

def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def _new_tokens() -> tuple[str, str]:
    return api_keys.PREFIX + secrets.token_urlsafe(30), "ekr_" + secrets.token_urlsafe(36)


def _response(access: str, refresh: str, scopes: list[str]) -> dict[str, Any]:
    return {"access_token": access, "token_type": "Bearer", "expires_in": int(ACCESS_LIFETIME.total_seconds()), "refresh_token": refresh, "scope": " ".join(scopes)}


async def exchange(form: dict[str, str]) -> dict[str, Any]:
    """Trade the one-use code and the PKCE proof for tokens. A code used a second time closes the connection it made."""
    client = await _client(form.get("client_id", ""))
    if client is None:
        raise OAuthError("invalid_client", "This app is not known.", 401)
    code, verifier = form.get("code", ""), form.get("code_verifier", "")
    if not code or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier or ""):
        raise OAuthError("invalid_request", "Send the code and a code_verifier of 43 to 128 characters.")
    resource = form.get("resource", "")
    if resource and resource.rstrip("/") != resource_url():
        raise OAuthError("invalid_target", "That resource is not this server.")
    doc = await _codes().find_one({"code_hash": _hash(code)})
    if doc is None or doc["client_id"] != client["client_id"]:
        raise OAuthError("invalid_grant", "That code is not valid.")
    if doc.get("used"):
        if doc.get("session_id"):
            await api_keys._keys().update_one({"id": doc["session_id"]}, {"$set": {"revoked": True, "revoked_at": _now()}})
        raise OAuthError("invalid_grant", "That code was already used, so the connection it made was closed.")
    if (_aware(doc.get("expires_at")) or _now()) <= _now():
        raise OAuthError("invalid_grant", "That code has run out.")
    if form.get("redirect_uri") != doc["redirect_uri"]:
        raise OAuthError("invalid_grant", "The redirect address does not match the sign-in.")
    if not hmac.compare_digest(_challenge(verifier), doc["code_challenge"]):
        raise OAuthError("invalid_grant", "The code_verifier does not match.")
    claimed = await _codes().update_one({"code_hash": doc["code_hash"], "used": False}, {"$set": {"used": True, "used_at": _now()}})
    if not claimed.modified_count:
        raise OAuthError("invalid_grant", "That code was already used.")
    access, refresh = _new_tokens()
    now = _now()
    session_id = uuid4().hex
    await api_keys._keys().insert_one({
        "id": session_id, "workspace_id": doc["workspace_id"], "name": doc["client_name"], "prefix": access[: len(api_keys.PREFIX) + 6], "hash": api_keys._hash(access),
        "scopes": [], "tool_scopes": list(doc["scopes"]), "capabilities": [], "credit_cap": None, "expires_at": now + ACCESS_LIFETIME, "created_by": doc["user_id"],
        "created_at": now, "last_used_at": None, "revoked": False, "surface": "mcp", "client_label": doc["client_name"],
        "oauth": {"client_id": client["client_id"], "refresh_hash": _hash(refresh), "refresh_expires_at": now + timedelta(days=sessions.SESSION_DAYS)},
    })
    await _codes().update_one({"code_hash": doc["code_hash"]}, {"$set": {"session_id": session_id}})
    return _response(access, refresh, doc["scopes"])


async def refresh(form: dict[str, str]) -> dict[str, Any]:
    """New access and refresh tokens. The refresh token just used stops working; showing it again later closes the whole connection."""
    client = await _client(form.get("client_id", ""))
    if client is None:
        raise OAuthError("invalid_client", "This app is not known.", 401)
    old = form.get("refresh_token", "")
    if not old:
        raise OAuthError("invalid_request", "Send the refresh_token.")
    keys = api_keys._keys()
    row = await keys.find_one({"oauth.refresh_hash": _hash(old)})
    if row is None:
        reused = await keys.find_one({"oauth.prev_refresh_hash": _hash(old)})
        if reused is not None and reused["oauth"].get("client_id") == client["client_id"]:
            rotated = _aware(reused["oauth"].get("rotated_at"))
            if not rotated or (_now() - rotated).total_seconds() > REFRESH_GRACE_SECONDS:
                await keys.update_one({"id": reused["id"]}, {"$set": {"revoked": True, "revoked_at": _now()}})
        raise OAuthError("invalid_grant", "That refresh token is not valid any more. Connect again.")
    if row["oauth"].get("client_id") != client["client_id"] or row.get("revoked") or (_aware(row["oauth"].get("refresh_expires_at")) or _now()) <= _now():
        raise OAuthError("invalid_grant", "This connection has ended. Connect again.")
    access, new_refresh = _new_tokens()
    now = _now()
    done = await keys.update_one(
        {"id": row["id"], "oauth.refresh_hash": _hash(old), "revoked": {"$ne": True}},
        {"$set": {"hash": api_keys._hash(access), "prefix": access[: len(api_keys.PREFIX) + 6], "expires_at": now + ACCESS_LIFETIME, "oauth.refresh_hash": _hash(new_refresh),
                  "oauth.prev_refresh_hash": _hash(old), "oauth.rotated_at": now}},
    )
    if not done.modified_count:
        raise OAuthError("invalid_grant", "That refresh token was just used. Connect again if this keeps happening.")
    return _response(access, new_refresh, list(row.get("tool_scopes") or []))


async def revoke(token: str) -> None:
    """Sign the connection out, whichever token is sent. An unknown token is a quiet success (RFC 7009)."""
    if not token:
        return
    h = _hash(token)
    keys = api_keys._keys()
    row = await keys.find_one({"$or": [{"hash": api_keys._hash(token)}, {"oauth.refresh_hash": h}], "surface": "mcp", "oauth": {"$exists": True}})
    if row:
        await keys.update_one({"id": row["id"]}, {"$set": {"revoked": True, "revoked_at": _now()}})
