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
from app.api.v1 import audio, text, video
from app.api.v1 import auth as auth_router
from app.api.v1 import brand as brand_router
from app.api.v1 import onboarding_draft as drafts_router
from app.api.v1 import users as users_router
from app.api.v1 import audio_assets, content, destinations, image_assets, media, oauth, publish
from app.api.v1 import presets as presets_router
from app.api.v1 import campaigns as campaigns_router
from app.api.v1 import runs as runs_router
from app.core.config import settings
from app.core.origins import build_origins
from app.api.v1 import workspace, invites
from app.core.logger import logger, setup_logging
from app.core.middleware import MaxBodySizeMiddleware, RequestLoggingMiddleware, limiter
from app.db.mongo import create_indexes, get_client as get_mongo_client
from app.db.migrations import run_startup_migrations
from app.db.redis import get_redis
from app.shared.llm import llm_health_check
from app.api.v1 import text_stream
from app.api.v1 import activity as activity_router
from app.api.v1 import assistant as assistant_router
from app.api.v1 import supervisor as supervisor_router
from app.db.redis import close_redis
from app.api.v1 import analytics as analytics_router
from app.api.v1 import search as search_router
from app.api.v1 import share as share_router
from app.api.v1 import platforms as platforms_router
from app.api.v1 import ops_platforms as ops_platforms_router
from app.api.v1 import ops_platform_lifecycle as ops_platform_lifecycle_router
from app.api.v1 import ops_platform_connections as ops_platform_connections_router
from app.api.v1 import ops_platform_insights as ops_platform_insights_router
from app.api.v1 import content_guard as content_guard_module
from app.api.v1 import ops_banners as ops_banners_module
from app.api.v1 import ops_cohorts as ops_cohorts_router
from app.api.v1 import ops_ai_budget as ops_ai_budget_router
from app.api.v1 import ops_llm_health as ops_llm_health_router
from app.api.v1 import support as support_router
from app.api.v1 import support_assistant as support_assistant_router
from app.api.v1 import ops_support as ops_support_router
from app.api.v1 import ops_support_tools as ops_support_tools_router
from app.api.v1 import ops_support_incidents as ops_support_incidents_router

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

    # Loud, fail-fast-adjacent check for the exact misconfiguration that has
    # already caused a real incident here (see app/core/auth.py::set_auth_cookies'
    # docstring and this file's CORS setup below): ENVIRONMENT=production with
    # no PRODUCTION_DOMAIN/FRONTEND_URL set silently degrades cookie auth to
    # SameSite=Lax across a genuinely cross-site deployment (Vercel frontend,
    # Render backend) — login appears to succeed (Set-Cookie is present) but
    # the cookie is never attached to the next request, so the user is
    # bounced back to login in an infinite loop. This used to fail silently;
    # now it's impossible to miss in the Render deploy logs.
    if settings.ENVIRONMENT == "production" and not (settings.PRODUCTION_DOMAIN or settings.FRONTEND_URL):
        logger.error(
            "STARTUP MISCONFIGURATION: ENVIRONMENT=production but neither "
            "PRODUCTION_DOMAIN nor FRONTEND_URL is set. Auth cookies will use "
            "SameSite=None (correct), but CORS will have no allowed origin "
            "beyond ALLOWED_ORIGINS, and the deployed frontend's login will "
            "loop indefinitely (Set-Cookie appears to work, then every "
            "following request silently has no cookie). Set PRODUCTION_DOMAIN "
            "or FRONTEND_URL in Render → Environment to the exact frontend "
            "origin (e.g. https://your-app.vercel.app) before this is usable."
        )

    from app.core.tracing import init_tracing
    init_tracing()

    get_mongo_client()
    await create_indexes()
    await run_startup_migrations()
    logger.info("MongoDB connected, indexes created, migrations applied")

    from app.platforms.base import import_all as import_all_platforms
    import_all_platforms()
    logger.info("Platform registry loaded")

    from app.shared import pipeline_runs
    from app.shared.job_actions import register_all as register_job_actions
    from app.shared.jobs import resume_interrupted

    register_job_actions()
    resumed = await resume_interrupted()
    if resumed:
        logger.info("%d safe-to-repeat background jobs were started again after the restart.", resumed)
    interrupted = await pipeline_runs.fail_interrupted()
    if interrupted:
        logger.warning("%d background runs were interrupted by the restart and were marked failed.", interrupted)

    from app.pipelines.media.image_generation import image_tiers_in_use
    if not image_tiers_in_use():
        logger.warning("No picture service is switched on, so every picture will be a text card.")

    # Start scheduler — every background job (publishing, campaigns, token
    # renewal, analytics, Remy/Odette, autonomy) is registered from one list,
    # app/workers/jobs.py. It runs in-process on this web service (no deployed
    # plan includes a spare background-worker instance); agent_worker.py can
    # run the same list as a separate arq process if that ever changes.
    from app.workers import inprocess as agent_workers
    await agent_workers.start(scheduler)

    scheduler.start()
    logger.info("Background workers started")
    try:
        from app.shared.llm_health.recorder import recorder as llm_recorder
        llm_recorder.start()
    except Exception as exc:  # noqa: BLE001 - the health log must never stop the app from starting
        logger.error("LLM health recorder did not start: %s", exc)

    yield

    logger.info("Shutting down application...")
    scheduler.shutdown()
    await agent_workers.stop()
    try:
        from app.shared.llm_health.recorder import recorder as llm_recorder
        await llm_recorder.stop()
    except Exception:  # noqa: BLE001
        pass
    from app.shared.activity import live as activity_live
    await activity_live.stop()
    from app.agents.text import session_relay
    await session_relay.stop_listener()
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
    {"name": "Activity", "description": "Activity Log — Active lane (Remy/Odette items awaiting a decision) and Passive lane (record of work done), plus a live SSE stream."},
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

# In production the primary domain falls back to FRONTEND_URL when PRODUCTION_DOMAIN isn't set (the two are easy to set independently and
# forget one, which once silently reopened a CORS gap), and the app's own addresses are always allowed (see app/core/origins.py).
origins = build_origins(
    production=settings.ENVIRONMENT == "production",
    production_domain=settings.PRODUCTION_DOMAIN,
    frontend_url=settings.FRONTEND_URL,
    allowed=settings.ALLOWED_ORIGINS,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "Retry-After"],
)

# Outermost middleware — runs before CORS/rate-limiting/logging, so an
# oversized body is rejected before any of that work happens.
app.add_middleware(MaxBodySizeMiddleware, allowed_origins=origins)


# --- Exception Handlers ---
# Both add the CORS headers themselves: an unhandled error is answered outside the CORS layer, and without them the browser would
# report a CORS failure instead of the real problem (see app/core/errors.py).
from app.core.errors import make_handlers  # noqa: E402

_rate_limit_handler, _unhandled_handler = make_handlers(origins)
app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
app.add_exception_handler(Exception, _unhandled_handler)


# --- Routers ---
app.include_router(auth_router.router,    prefix="/api/v1/auth",       tags=["Auth"])
app.include_router(oauth.router,          prefix="/api/v1/oauth",      tags=["OAuth"])
app.include_router(workspace.router, prefix="/api/v1/workspaces", tags=["Workspace"])
from app.api.v1 import waitlist as waitlist_router
app.include_router(waitlist_router.router, prefix="/api/v1/waitlist", tags=["Waitlist"])
from app.api.v1 import contact as contact_router
app.include_router(contact_router.router, prefix="/api/v1/contact", tags=["Contact"])
from app.api.v1 import ops_leads as ops_leads_router
app.include_router(ops_leads_router.router, prefix="/api/v1/ops/leads", tags=["Ops Leads"])
from app.api.v1 import ops_invites as ops_invites_router
app.include_router(ops_invites_router.router, prefix="/api/v1/ops/leads", tags=["Ops Leads"])
from app.api.v1 import jobs as jobs_router
app.include_router(jobs_router.router, prefix="/api/v1/jobs", tags=["Jobs"])
app.include_router(invites.router, prefix="/api/v1/invites", tags=["Invites"])
app.include_router(publish.router,        prefix="/api/v1/publish",    tags=["Publish"])
app.include_router(destinations.router,    prefix="/api/v1/destinations", tags=["Destinations"])
app.include_router(users_router.router,   prefix="/api/v1/users",      tags=["Users"])
app.include_router(brand_router.router,   prefix="/api/v1/brand",      tags=["Brand"])
app.include_router(drafts_router.router,  prefix="/api/v1/onboarding", tags=["Drafts"])
app.include_router(content.router,        prefix="/api/v1/content",    tags=["Content"])
app.include_router(media.router,          prefix="/api/v1/media",      tags=["Media"])
app.include_router(presets_router.router, prefix="/api/v1/presets",    tags=["Presets"])
app.include_router(campaigns_router.router, prefix="/api/v1/campaigns", tags=["Campaigns"])
app.include_router(runs_router.router, prefix="/api/v1/runs", tags=["Runs"])
app.include_router(text.router,           prefix="/api/v1/text",       tags=["Text Pipeline"])
app.include_router(audio.router,          prefix="/api/v1/audio",      tags=["Audio"])
app.include_router(video.router,          prefix="/api/v1/video",      tags=["Video"])
# The old app.api.v1.image decoy (every handler was a placeholder — see
# pow/audio_image_pipeline/00-overview.md Finding #1) was never registered
# here and its files (app/api/v1/image.py, app/pipelines/image/*,
# app/agents/image/*) were deleted 2026-09-28 — image_assets.router below
# is the real, only Image pipeline now.
app.include_router(image_assets.router, prefix="/api/v1/image-assets", tags=["Image Assets"])
app.include_router(audio_assets.router, prefix="/api/v1/audio-assets", tags=["Audio Assets"])
app.include_router(analytics_router.router, prefix="/api/v1/analytics", tags=["Analytics"])
app.include_router(search_router.router, prefix="/api/v1/search", tags=["Search"])
app.include_router(share_router.router, prefix="/api/v1/share", tags=["Share"])
app.include_router(assistant_router.router, prefix="/api/v1/assistant", tags=["Assistant"])
app.include_router(supervisor_router.router, prefix="/api/v1/supervisor", tags=["Supervisor"])
app.include_router(platforms_router.router, prefix="/api/v1/platforms", tags=["Platforms"])
# The settings routes come first: their fixed paths ("/configs", "/{key}/config") must be matched before the
# overview router's "/{key}" detail route would take them.
app.include_router(ops_platforms_router.router, prefix="/api/v1/ops/platforms", tags=["Ops"])
app.include_router(ops_platform_lifecycle_router.router, prefix="/api/v1/ops/platforms", tags=["Ops"])
app.include_router(ops_platform_connections_router.router, prefix="/api/v1/ops/platforms", tags=["Ops"])
app.include_router(ops_platform_insights_router.router, prefix="/api/v1/ops/platforms", tags=["Ops"])
app.include_router(content_guard_module.router, prefix="/api/v1/content-guard", tags=["Content Guard"])
app.include_router(content_guard_module.ops_router, prefix="/api/v1/ops/content-safety", tags=["Ops"])
app.include_router(ops_banners_module.router, prefix="/api/v1/ops/banners", tags=["Ops"])
app.include_router(ops_cohorts_router.router, prefix="/api/v1/ops/cohorts", tags=["Ops"])
app.include_router(ops_ai_budget_router.router, prefix="/api/v1/ops/ai", tags=["Ops"])
app.include_router(ops_llm_health_router.router, prefix="/api/v1/ops/llm", tags=["Ops"])
app.include_router(support_router.router, prefix="/api/v1/support", tags=["Support"])
app.include_router(support_assistant_router.router, prefix="/api/v1/support", tags=["Support"])
app.include_router(ops_support_router.router, prefix="/api/v1/ops/support", tags=["Ops"])
app.include_router(ops_support_tools_router.router, prefix="/api/v1/ops/support", tags=["Ops"])
app.include_router(ops_support_incidents_router.router, prefix="/api/v1/ops/support", tags=["Ops"])
app.include_router(activity_router.router, prefix="/api/v1/activity", tags=["Activity"])
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
    """Deep readiness check — pings Mongo, Redis, and both LLM providers.
    Point an external uptime monitor here, not Render's health check.

    Mongo/Redis are hard dependencies — every request needs them, so a
    failure there flips overall status to "not ready" (503). Groq/Gemini
    are not: this app works fine (auth, workspace, invites, settings —
    everything but AI generation) with both LLM providers down, so an
    LLM outage is reported in the body for monitoring/alerting to see and
    page on distinctly, without marking the whole service down over a
    degradation that leaves most of it working. Previously invisible to
    any monitoring — llm_health_check() existed and was never called.
    """
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

    llm_status = await llm_health_check()
    llm_problems = [
        f"{provider}: {info.get('detail', 'unknown error')}"
        for provider, info in llm_status.items()
        if info.get("status") != "ok"
    ]

    if problems:
        return JSONResponse(
            status_code=503,
            content={"status": "not ready", "problems": problems, "llm": llm_status},
        )
    return JSONResponse(
        status_code=200,
        content={
            "status": "ready",
            "llm": llm_status,
            "llm_degraded": bool(llm_problems),
        },
    )


# --- Docs (Scalar) ---
_scalar_dependencies = [] if _DOCS_PUBLIC else [Depends(_verify_docs_auth)]


@app.get("/scalar", include_in_schema=False, dependencies=_scalar_dependencies)
async def scalar_docs() -> HTMLResponse:
    return get_scalar_api_reference(
        openapi_url=_OPENAPI_PATH,
        title=app.title,
    )
