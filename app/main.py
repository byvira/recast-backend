"""FastAPI application factory — CORS, middleware, routers, lifecycle events."""

import secrets as _secrets
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from scalar_fastapi import get_scalar_api_reference
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.api.v1 import audio, image, text, video
from app.api.v1 import auth as auth_router
from app.api.v1 import brand as brand_router
from app.api.v1 import onboarding_draft as drafts_router
from app.api.v1 import users as users_router
from app.api.v1 import content, oauth, publish
from app.core.config import settings
from app.api.v1 import workspace, invites
from app.core.logger import logger, setup_logging
from app.core.middleware import MaxBodySizeMiddleware, RequestLoggingMiddleware, limiter
from app.db.mongo import create_indexes, get_client as get_mongo_client
from app.db.migrations import run_startup_migrations
from app.db.redis import get_redis
from app.api.v1 import text_stream
from app.api.v1 import assistant as assistant_router
from app.api.v1 import supervisor as supervisor_router
from app.db.redis import close_redis
from app.workers.scheduled_posts import process_scheduled_posts
from app.workers.token_refresh import refresh_expiring_tokens
from app.pipelines.analytics.scheduler import refresh_analytics
from app.api.v1 import analytics as analytics_router

setup_logging()


def _release_sha() -> str:
    """Best-effort release identifier for Sentry — Render sets this env var
    on every deploy; fall back to the local git SHA outside Render."""
    import os
    import subprocess

    render_sha = os.environ.get("RENDER_GIT_COMMIT")
    if render_sha:
        return render_sha[:12]
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:  # noqa: BLE001
        return "unknown"


_SENTRY_SCRUB_HEADERS = {"authorization", "cookie", "set-cookie"}
_SENTRY_SCRUB_KEYS = {"access_token", "refresh_token", "password", "otp"}


def _sentry_before_send(event: dict, hint: dict) -> dict | None:
    """Scrub auth headers/tokens before an event leaves the process."""
    request = event.get("request")
    if request and isinstance(request.get("headers"), dict):
        for key in list(request["headers"].keys()):
            if key.lower() in _SENTRY_SCRUB_HEADERS:
                request["headers"][key] = "[Filtered]"

    def _scrub(obj):
        if isinstance(obj, dict):
            return {
                k: ("[Filtered]" if k.lower() in _SENTRY_SCRUB_KEYS else _scrub(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [_scrub(v) for v in obj]
        return obj

    for field in ("extra", "contexts"):
        if field in event:
            event[field] = _scrub(event[field])

    return event


if settings.SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        release=_release_sha(),
        traces_sample_rate=0.1,
        integrations=[StarletteIntegration(), FastApiIntegration()],
        before_send=_sentry_before_send,
    )
    logger.info("Sentry initialised (environment=%s, release=%s)", settings.ENVIRONMENT, _release_sha())

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Handle startup and shutdown events."""

    logger.info("Starting application...")

    from app.core.tracing import init_tracing
    init_tracing()

    get_mongo_client()
    await create_indexes()
    await run_startup_migrations()
    logger.info("MongoDB connected, indexes created, migrations applied")

    # Start scheduler
    scheduler.add_job(process_scheduled_posts, "interval", minutes=1, id="scheduled_posts")
    scheduler.add_job(refresh_expiring_tokens, "interval", hours=24, id="token_refresh")
    scheduler.add_job(refresh_analytics,       "interval", hours=6,    id="analytics_refresh") 
    scheduler.start()
    logger.info("Background workers started")

    yield

    logger.info("Shutting down application...")
    scheduler.shutdown()
    from app.agents.supervisor.service import close_arq_pool
    await close_arq_pool()
    get_mongo_client().close()
    await close_redis()
    logger.info("Connections closed successfully")


OPENAPI_TAGS = [
    {"name": "Auth", "description": "OTP-based signup, login, and session token management."},
    {"name": "OAuth", "description": "Connect and manage third-party platform accounts (Meta, Google, Threads, Bluesky, etc.)."},
    {"name": "Workspace", "description": "Create and manage team workspaces."},
    {"name": "Invites", "description": "Invite and manage workspace members."},
    {"name": "Publish", "description": "Publish and schedule content pieces to connected platforms."},
    {"name": "Users", "description": "User profile management."},
    {"name": "Brand", "description": "Brand onboarding and brand profile management."},
    {"name": "Drafts", "description": "Onboarding draft persistence."},
    {"name": "Content", "description": "Manage generated content pieces — review, approve, reject, schedule, and version history."},
    {"name": "Text Pipeline", "description": "AI-powered text generation, repurposing, and refinement."},
    {"name": "Audio", "description": "Audio content generation pipeline."},
    {"name": "Video", "description": "Video content generation pipeline."},
    {"name": "Image", "description": "Image content generation pipeline."},
    {"name": "Analytics", "description": "Cross-platform post performance analytics and insights."},
    {"name": "Assistant", "description": "Per-member personal assistant — voice persona, drift signals, and draft alignment."},
    {"name": "Supervisor", "description": "Workspace supervisor (admin-only) — insights, flags, and dashboard for workspace health."},
    {"name": "Pipeline", "description": "Real-time streaming endpoints for the text generation pipeline."},
    {"name": "Health", "description": "Service health checks."},
]

# Public docs (/docs, /redoc, /scalar, /openapi.json) are a full recon map of
# every route, param and auth flow. Open in development; behind HTTP Basic
# auth in production. If DOCS_USERNAME/DOCS_PASSWORD are unset, docs are
# simply unreachable in production rather than falling open.
_DOCS_PUBLIC = settings.ENVIRONMENT != "production"
_OPENAPI_PATH = "/openapi.json"

app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "Agentic SaaS platform that repurposes content across formats "
        "(text, audio, video, image) using AI pipelines. Provides OTP-based "
        "authentication, OAuth platform connections, AI content generation "
        "and repurposing pipelines, scheduling and publishing, and "
        "cross-platform analytics."
    ),
    version="0.1.0",
    openapi_tags=OPENAPI_TAGS,
    servers=(
        [{"url": settings.PRODUCTION_DOMAIN, "description": "Production"}]
        if settings.ENVIRONMENT == "production" and settings.PRODUCTION_DOMAIN
        else None
    ),
    docs_url="/docs" if _DOCS_PUBLIC else None,
    redoc_url="/redoc" if _DOCS_PUBLIC else None,
    openapi_url=_OPENAPI_PATH if _DOCS_PUBLIC else None,
    lifespan=lifespan,
)

_docs_security = HTTPBasic()


def _verify_docs_auth(credentials: HTTPBasicCredentials = Depends(_docs_security)) -> None:
    """Gate /docs, /redoc, /scalar and /openapi.json in production.

    Denies unconditionally if DOCS_USERNAME/DOCS_PASSWORD aren't both set —
    docs must be explicitly enabled, never open by a missing-config accident.
    """
    valid_username = bool(settings.DOCS_USERNAME) and _secrets.compare_digest(
        credentials.username, settings.DOCS_USERNAME
    )
    valid_password = bool(settings.DOCS_PASSWORD) and _secrets.compare_digest(
        credentials.password, settings.DOCS_PASSWORD
    )
    if not (valid_username and valid_password):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


if not _DOCS_PUBLIC:
    @app.get(_OPENAPI_PATH, include_in_schema=False)
    async def protected_openapi(_: None = Depends(_verify_docs_auth)) -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/docs", include_in_schema=False)
    async def protected_docs(_: None = Depends(_verify_docs_auth)) -> HTMLResponse:
        return get_swagger_ui_html(openapi_url=_OPENAPI_PATH, title=f"{app.title} - Docs")

    @app.get("/redoc", include_in_schema=False)
    async def protected_redoc(_: None = Depends(_verify_docs_auth)) -> HTMLResponse:
        return get_redoc_html(openapi_url=_OPENAPI_PATH, title=f"{app.title} - ReDoc")

# --- Middleware ---
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(RequestLoggingMiddleware)

if settings.ENVIRONMENT == "production":
    origins = [settings.PRODUCTION_DOMAIN] if settings.PRODUCTION_DOMAIN else []
    origins += [o for o in settings.ALLOWED_ORIGINS if o not in origins]
else:
    origins = list(settings.ALLOWED_ORIGINS)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Outermost middleware — runs before CORS/rate-limiting/logging, so an
# oversized body is rejected before any of that work happens.
app.add_middleware(MaxBodySizeMiddleware)


# --- Exception Handlers ---
@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    logger.warning("Rate limit exceeded | PATH=%s | IP=%s",
                   request.url.path,
                   request.client.host if request.client else "unknown")
    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded. Please slow down."})


# --- Routers ---
app.include_router(auth_router.router,    prefix="/api/v1/auth",       tags=["Auth"])
app.include_router(oauth.router,          prefix="/api/v1/oauth",      tags=["OAuth"])
app.include_router(workspace.router, prefix="/api/v1/workspaces", tags=["Workspace"])
app.include_router(invites.router, prefix="/api/v1/invites", tags=["Invites"])
app.include_router(publish.router,        prefix="/api/v1/publish",    tags=["Publish"])
app.include_router(users_router.router,   prefix="/api/v1/users",      tags=["Users"])
app.include_router(brand_router.router,   prefix="/api/v1/brand",      tags=["Brand"])
app.include_router(drafts_router.router,  prefix="/api/v1/onboarding", tags=["Drafts"])
app.include_router(content.router,        prefix="/api/v1/content",    tags=["Content"])
app.include_router(text.router,           prefix="/api/v1/text",       tags=["Text Pipeline"])
app.include_router(audio.router,          prefix="/api/v1/audio",      tags=["Audio"])
app.include_router(video.router,          prefix="/api/v1/video",      tags=["Video"])
app.include_router(image.router,          prefix="/api/v1/image",      tags=["Image"])
app.include_router(analytics_router.router, prefix="/api/v1/analytics", tags=["Analytics"])
app.include_router(assistant_router.router, prefix="/api/v1/assistant", tags=["Assistant"])
app.include_router(supervisor_router.router, prefix="/api/v1/supervisor", tags=["Supervisor"])
app.include_router(
    text_stream.router,
    prefix="/api/v1/pipeline",
    tags=["Pipeline"]
)

# --- Health ---
@app.get("/health", tags=["Health"])
async def health_check() -> dict[str, str]:
    """Shallow liveness check — no DB/Redis touch. Point Render's own health
    check here so a slow dependency never kills the instance."""
    return {"status": "ok"}


@app.get("/health/ready", tags=["Health"])
async def health_ready() -> JSONResponse:
    """Deep readiness check — pings Mongo and Redis. Point an external uptime
    monitor here, not Render's health check."""
    problems: list[str] = []

    try:
        await get_mongo_client().get_default_database().command("ping")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"mongo: {exc}")

    try:
        redis = await get_redis()
        await redis.ping()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"redis: {exc}")

    if problems:
        return JSONResponse(status_code=503, content={"status": "not ready", "problems": problems})
    return JSONResponse(status_code=200, content={"status": "ready"})


# --- Docs (Scalar) ---
_scalar_dependencies = [] if _DOCS_PUBLIC else [Depends(_verify_docs_auth)]


@app.get("/scalar", include_in_schema=False, dependencies=_scalar_dependencies)
async def scalar_docs() -> HTMLResponse:
    return get_scalar_api_reference(
        openapi_url=_OPENAPI_PATH,
        title=app.title,
    )
