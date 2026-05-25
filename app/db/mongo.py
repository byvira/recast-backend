"""Async MongoDB client, collection helpers, and index management via Motor."""

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


# ── Module-level collection shortcuts for direct import ───────────────────────
users: AsyncIOMotorCollection = get_client().get_default_database()["users"]
text: AsyncIOMotorCollection = get_client().get_default_database()["text"]
brand_profiles: AsyncIOMotorCollection = get_client().get_default_database()["brand_profiles"]
onboarding_drafts: AsyncIOMotorCollection = get_client().get_default_database()["onboarding_drafts"] 



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


async def create_indexes() -> None:
    """Create all MongoDB indexes on startup.

    Safe to call multiple times — Motor's create_index is idempotent when
    the index definition matches an existing one.
    """
    # Users collection — unique sparse indexes allow multiple docs without field
    await users.create_index("email", unique=True, sparse=True)
    await users.create_index("phone", unique=True, sparse=True)
    await users.create_index("username", unique=True)
    await users.create_index("auth_identifiers")
    

    # Brand profiles collection
    await brand_profiles.create_index("user_id")
    await brand_profiles.create_index([("user_id", 1), ("is_complete", 1)])
    await brand_profiles.create_index([("user_id", 1), ("brand_type", 1)])
    await onboarding_drafts.create_index("user_id", unique=True)
    await onboarding_drafts.create_index("is_complete") 
