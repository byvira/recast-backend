"""Async MongoDB client and collection helpers via Motor."""

from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorCollection,
    AsyncIOMotorDatabase,
)

from app.core.config import settings

_client: AsyncIOMotorClient | None = None


def get_client() -> AsyncIOMotorClient:
    """Return the singleton Motor client, creating it on first access.

    Returns:
        Configured AsyncIOMotorClient instance.
    """
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.MONGODB_URL)
    return _client


def get_db() -> AsyncIOMotorDatabase:
    """Return the default database derived from the connection URL.

    The database name must be embedded in ``MONGODB_URL``
    (e.g. ``mongodb://localhost:27017/saas_dev``).

    Returns:
        AsyncIOMotorDatabase for the application.
    """
    return get_client().get_default_database()


def get_users_collection() -> AsyncIOMotorCollection:
    """Return the users collection."""
    return get_db()["users"]


def get_campaigns_collection() -> AsyncIOMotorCollection:
    """Return the campaigns collection."""
    return get_db()["campaigns"]


def get_brand_profiles_collection() -> AsyncIOMotorCollection:
    """Return the brand_profiles collection."""
    return get_db()["brand_profiles"]


def get_jobs_collection() -> AsyncIOMotorCollection:
    """Return the jobs collection."""
    return get_db()["jobs"]


def get_text_outputs_collection() -> AsyncIOMotorCollection:
    """Return the text_outputs collection."""
    return get_db()["text_outputs"]


def get_audio_outputs_collection() -> AsyncIOMotorCollection:
    """Return the audio_outputs collection."""
    return get_db()["audio_outputs"]


def get_video_outputs_collection() -> AsyncIOMotorCollection:
    """Return the video_outputs collection."""
    return get_db()["video_outputs"]


def get_image_outputs_collection() -> AsyncIOMotorCollection:
    """Return the image_outputs collection."""
    return get_db()["image_outputs"]
