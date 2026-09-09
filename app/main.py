"""FastAPI application factory — CORS, middleware, routers, lifecycle events."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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
from app.core.middleware import RequestLoggingMiddleware, limiter
from app.db.mongo import create_indexes, get_client as get_mongo_client
from app.api.v1 import text_stream
from app.db.redis import close_redis
from app.workers.scheduled_posts import process_scheduled_posts
from app.workers.token_refresh import refresh_expiring_tokens
from app.pipelines.analytics.scheduler import refresh_analytics
from app.api.v1 import analytics as analytics_router

setup_logging()

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Handle startup and shutdown events."""

    logger.info("Starting application...")

    get_mongo_client()
    await create_indexes()
    logger.info("MongoDB connected and indexes created")

    # Start scheduler
    scheduler.add_job(process_scheduled_posts, "interval", minutes=1, id="scheduled_posts")
    scheduler.add_job(refresh_expiring_tokens, "interval", hours=24, id="token_refresh")
    scheduler.add_job(refresh_analytics,       "interval", hours=6,    id="analytics_refresh") 
    scheduler.start()
    logger.info("Background workers started")

    yield

    logger.info("Shutting down application...")
    scheduler.shutdown()
    get_mongo_client().close()
    await close_redis()
    logger.info("Connections closed successfully")


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# --- Middleware ---
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(RequestLoggingMiddleware)

if settings.ENVIRONMENT == "production":
    origins = [settings.PRODUCTION_DOMAIN] if settings.PRODUCTION_DOMAIN else []
else:
    origins = ["http://localhost:3000", "http://localhost:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
app.include_router(workspace.router, prefix="/workspace", tags=["workspace"])
app.include_router(invites.router, prefix="/invites", tags=["invites"])
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
app.include_router(
    text_stream.router,
    prefix="/api/v1/pipeline",
    tags=["pipeline"]
)

# --- Health ---
@app.get("/health", tags=["Health"])
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
