"""FastAPI application factory — CORS, middleware, routers, lifecycle events."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import audio, brand, image, publish, text, video
from app.core.config import settings
from app.core.middleware import RequestLoggingMiddleware
from app.db.mongo import get_client as get_mongo_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup and shutdown of shared resources.

    Initialises the Motor MongoDB client on startup and closes it cleanly on
    shutdown so connections are not leaked between restarts.
    """
    get_mongo_client()
    yield
    get_mongo_client().close()


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestLoggingMiddleware)

app.include_router(text.router, prefix="/api/v1/text", tags=["text"])
app.include_router(audio.router, prefix="/api/v1/audio", tags=["audio"])
app.include_router(video.router, prefix="/api/v1/video", tags=["video"])
app.include_router(image.router, prefix="/api/v1/image", tags=["image"])
app.include_router(brand.router, prefix="/api/v1/brand", tags=["brand"])
app.include_router(publish.router, prefix="/api/v1/publish", tags=["publish"])


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    """Return service health status — no authentication required."""
    return {"status": "ok"}
