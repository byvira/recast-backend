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


async def signup_new_user(client: httpx.AsyncClient, name: str = "Test User") -> dict:
    """Full OTP -> signup flow for a brand-new user on the given client.

    Returns the user profile dict from the signup response. The client ends
    up holding the resulting access_token/refresh_token HttpOnly cookies,
    same as a browser would.
    """
    email = unique_email()
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


@pytest.fixture
def signup_user(make_client):
    """Factory fixture: `client, profile = await signup_user()` signs up a
    brand-new user on a fresh client/cookie-jar."""

    async def _do(name: str = "Test User"):
        client = make_client()
        profile = await signup_new_user(client, name=name)
        return client, profile

    return _do
