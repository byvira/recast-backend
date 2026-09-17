"""Shared fixtures for the integration test suite.

Runs against a real, isolated MongoDB database (never the dev/prod one) and
the real Redis instance configured in .env. The test database name is
derived from MONGODB_URL with a `_test` suffix, dropped before the session
starts and dropped again once it finishes.

Deliberately does NOT run the app's full lifespan (app.main.lifespan) —
that also starts the APScheduler background workers (scheduled post
publishing, token refresh, analytics refresh), which would run against
whatever is in the test database on a timer during the test run. Index
creation is invoked directly instead.
"""

import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
import pytest_asyncio


def _test_mongo_url() -> str:
    """Derive an isolated `<db>_test` MongoDB URL from .env's MONGODB_URL.

    Reads the .env file directly rather than importing app.core.config, so
    this runs before any app module is imported. Never logs or returns the
    credential-bearing URL to a print/log call — only into the environment.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    contents = env_path.read_text(encoding="utf-8")
    match = re.search(r"^MONGODB_URL=(.+)$", contents, re.MULTILINE)
    if not match:
        raise RuntimeError(
            "MONGODB_URL not found in .env — required to run the integration suite."
        )
    base_url = match.group(1).strip()
    parts = urlsplit(base_url)
    db_name = parts.path.lstrip("/") or "saas_db"
    test_db_name = db_name if db_name.endswith("_test") else f"{db_name}_test"
    return urlunsplit((parts.scheme, parts.netloc, f"/{test_db_name}", parts.query, parts.fragment))


# Must happen before any `app.*` import — app.core.config.settings is built
# once, at import time, from the environment + .env.
os.environ["MONGODB_URL"] = _test_mongo_url()
os.environ.setdefault("ENVIRONMENT", "development")
# Never report test runs to the real Sentry project, and skip its background
# worker thread (which otherwise logs noisily on interpreter shutdown).
os.environ["SENTRY_DSN"] = ""

import httpx  # noqa: E402

from app.core.middleware import limiter  # noqa: E402
from app.db.mongo import create_indexes, get_client  # noqa: E402
from app.db.redis import get_redis  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """slowapi's in-memory limiter is keyed by IP, not identifier, and
    persists for the whole test process — without a reset, every test past
    the 5th request-otp call anywhere in the run would 429 regardless of
    using a fresh email each time."""
    limiter.reset()
    yield


def _assert_test_database(db_name: str) -> None:
    if not db_name.endswith("_test"):
        raise RuntimeError(
            f"Refusing to run the integration suite against non-test database {db_name!r}."
        )


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _test_database_lifecycle():
    """Drop the test database before the run and again after it finishes."""
    client = get_client()
    db_name = client.get_default_database().name
    _assert_test_database(db_name)

    await client.drop_database(db_name)
    await create_indexes()

    yield

    await client.drop_database(db_name)
    client.close()


@pytest_asyncio.fixture
async def make_client():
    """Factory for independent httpx AsyncClients (each its own cookie jar),
    so a test can hold two logged-in actors (e.g. a workspace owner and an
    invited member) at once without one session's cookies clobbering the
    other's."""
    clients: list[httpx.AsyncClient] = []

    def _make() -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        clients.append(client)
        return client

    yield _make

    for client in clients:
        await client.aclose()


@pytest.fixture
def api_client(make_client):
    """A single default client for tests that only need one actor."""
    return make_client()


def unique_email() -> str:
    return f"test-{uuid4().hex}@example.com"


def unique_username() -> str:
    return f"user_{uuid4().hex[:12]}"


async def mark_otp_verified(identifier: str) -> None:
    """Set the `otp:{identifier}:verified` Redis flag directly, bypassing the
    request-otp/verify-otp round trip. Used only to set up a precondition
    (e.g. "OTP already verified") without tripping the per-identifier
    60-second request-otp cooldown that a real second round trip would hit."""
    redis = await get_redis()
    await redis.set(f"otp:{identifier}:verified", "1", ex=600)


async def read_otp_code(identifier: str) -> str:
    """Read the OTP directly out of Redis — there is no dev-mode API backdoor
    for it, and the real delivery channel (console/email/SMS) isn't
    observable from a test process."""
    redis = await get_redis()
    code = await redis.get(f"otp:{identifier}:code")
    if not code:
        raise RuntimeError(f"No OTP found in Redis for identifier {identifier!r}.")
    return code


async def signup_new_user(
    client: httpx.AsyncClient, name: str = "Test User", email: str | None = None
) -> dict:
    """Full OTP -> signup flow for a brand-new user on the given client.

    Returns the user profile dict from the signup response. The client ends
    up holding the resulting access_token/refresh_token HttpOnly cookies,
    same as a browser would. Pass `email` to sign up under a specific
    address (e.g. one an invite was already sent to) instead of a random one.
    """
    email = email or unique_email()
    res = await client.post(
        "/api/v1/auth/request-otp", json={"identifier": email, "channel": "email"}
    )
    assert res.status_code == 200, res.text

    otp = await read_otp_code(email)
    res = await client.post(
        "/api/v1/auth/verify-otp",
        json={"identifier": email, "otp": otp, "channel": "email"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["valid"] is True

    username = unique_username()
    res = await client.post(
        "/api/v1/auth/signup",
        json={
            "identifier": email,
            "channel": "email",
            "name": name,
            "username": username,
        },
    )
    assert res.status_code == 200, res.text
    return res.json()["user"]


async def create_workspace(client: httpx.AsyncClient, name: str, tier: str = "duo") -> str:
    """Create a workspace as the given (already-authenticated) client's user.
    Returns the new workspace_id."""
    res = await client.post("/api/v1/workspaces/", json={"name": name, "tier": tier})
    assert res.status_code == 201, res.text
    return res.json()["workspace_id"]


async def invite_and_accept(
    owner_client: httpx.AsyncClient,
    make_client,
    workspace_id: str,
    role: str,
) -> tuple[httpx.AsyncClient, dict]:
    """Invite a brand-new user to `workspace_id` as `role` and have them
    accept immediately. Returns (member_client, member_profile).

    accept_invite requires the accepting account's email to match the
    invited address, so the invitee signs up under that exact email."""
    invite_email = unique_email()
    res = await owner_client.post(
        f"/api/v1/invites/{workspace_id}", json={"email": invite_email, "role": role}
    )
    assert res.status_code == 201, res.text
    token = res.json()["token"]

    member_client = make_client()
    member_profile = await signup_new_user(member_client, email=invite_email)
    res = await member_client.post(f"/api/v1/invites/accept/{token}")
    assert res.status_code == 200, res.text
    return member_client, member_profile


@pytest.fixture
def signup_user(make_client):
    """Factory fixture: `client, profile = await signup_user()` signs up a
    brand-new user on a fresh client/cookie-jar."""

    async def _do(name: str = "Test User"):
        client = make_client()
        profile = await signup_new_user(client, name=name)
        return client, profile

    return _do


# ─────────────────────────────────────────────────────────────────────────────
# LLM mock layer — Module 2 (Content Pipeline)
# ─────────────────────────────────────────────────────────────────────────────
#
# This codebase's own established rule: no automated test ever calls a real
# LLM (see e.g. test_voice_suggestions.py, test_llm_shared.py). The pattern
# used everywhere else in this suite is per-test monkeypatch.setattr on
# whichever module actually imported call_llm/call_llm_structured — Python
# binds `from x import y` locally at import time, so patching
# app.shared.llm.call_llm itself does nothing; the *consuming* module's own
# bound reference is what actually runs at call time.
#
# Module 2 (chip-refine, chat-refine, score-hook, regenerate, repurpose)
# hits this in six different modules, so this fixture patches all of them
# in one call instead of every test file repeating the same import-and-patch
# boilerplate six times over.

_LLM_CONSUMER_MODULES = [
    "app.pipelines.text.chips",         # call_llm        — chip-refine
    "app.pipelines.text.refiner",       # call_llm_chat    — chat-refine
    "app.pipelines.text.scorer",        # call_llm_structured — score-hook
    "app.pipelines.text.generator",     # call_llm, call_llm_structured — generate/regenerate
    "app.pipelines.text.hook_agent",    # call_llm_structured — hook variants
    "app.pipelines.text.angles",        # call_llm_structured — angle variants (Feature 8)
    "app.pipelines.text.seo",           # call_llm_structured — SEO package
    "app.pipelines.text.repurpose",     # call_llm_structured — repurpose
    "app.pipelines.text.normalizer",    # call_llm, call_llm_structured — input normalisation
]


@pytest.fixture
def mock_llm(monkeypatch):
    """Reusable LLM mock for every Module 2 test — never a real Groq call.

    Patches call_llm / call_llm_structured / call_llm_chat across every
    known consumer module at once. Defaults to plausible non-empty
    responses so a test that doesn't care about the exact generated text
    still exercises real code (word counts, version writes, etc.) rather
    than tripping on emptiness. Override per test via the returned handle:

        async def test_x(mock_llm):
            mock_llm.set_structured({"refined": "New punchy content", "changed": True})
            res = await client.post(...)
    """
    import importlib

    state = {
        "plain": "Mocked generated content — realistic length for word/char counts.",
        "structured": {},
        "chat": "Mocked refined content.",
    }

    async def _fake_call_llm(*args, **kwargs):
        return state["plain"]

    async def _fake_call_llm_structured(*args, **kwargs):
        return state["structured"]

    async def _fake_call_llm_chat(*args, **kwargs):
        return state["chat"]

    for module_path in _LLM_CONSUMER_MODULES:
        module = importlib.import_module(module_path)
        if hasattr(module, "call_llm"):
            monkeypatch.setattr(module, "call_llm", _fake_call_llm)
        if hasattr(module, "call_llm_structured"):
            monkeypatch.setattr(module, "call_llm_structured", _fake_call_llm_structured)
        if hasattr(module, "call_llm_chat"):
            monkeypatch.setattr(module, "call_llm_chat", _fake_call_llm_chat)

    class _MockLLMHandle:
        def set_plain(self, value: str) -> None:
            state["plain"] = value

        def set_structured(self, value: dict) -> None:
            state["structured"] = value

        def set_chat(self, value: str) -> None:
            state["chat"] = value

    return _MockLLMHandle()
