"""Async MongoDB client, collection helpers, and index management via Motor."""

from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorCollection,
    AsyncIOMotorDatabase,
)

from app.core.config import settings

_client: AsyncIOMotorClient | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.MONGODB_URL)
    return _client


def get_db() -> AsyncIOMotorDatabase:
    return get_client().get_default_database()


# ── Existing collections ──────────────────────────────────────────────────────
users: AsyncIOMotorCollection = get_client().get_default_database()["users"]
text: AsyncIOMotorCollection = get_client().get_default_database()["text"]
brand_profiles: AsyncIOMotorCollection = get_client().get_default_database()["brand_profiles"]
onboarding_drafts: AsyncIOMotorCollection = get_client().get_default_database()["onboarding_drafts"]
# ── Workspace / tenancy collections ──────────────────────────────────────────
workspaces: AsyncIOMotorCollection = get_client().get_default_database()["workspaces"]
workspace_members: AsyncIOMotorCollection = get_client().get_default_database()["workspace_members"]
invites: AsyncIOMotorCollection = get_client().get_default_database()["invites"]
# Third-party platform tokens, scoped per workspace (replaces users.social_accounts[])
workspace_connections: AsyncIOMotorCollection = get_client().get_default_database()["workspace_connections"]

# ── Sprint 4 — Content storage collections ───────────────────────────────────
content_sessions: AsyncIOMotorCollection = get_client().get_default_database()["content_sessions"]
content_pieces: AsyncIOMotorCollection = get_client().get_default_database()["content_pieces"]
content_piece_versions: AsyncIOMotorCollection = get_client().get_default_database()["content_piece_versions"]
account_metrics: AsyncIOMotorCollection = get_client().get_default_database()["account_metrics"]
post_metrics: AsyncIOMotorCollection    = get_client().get_default_database()["post_metrics"]

# ── Two-layer agent architecture (personal assistant + workspace supervisor) ──
workspace_events: AsyncIOMotorCollection     = get_client().get_default_database()["workspace_events"]
member_personas: AsyncIOMotorCollection      = get_client().get_default_database()["member_personas"]
personal_signals: AsyncIOMotorCollection     = get_client().get_default_database()["personal_signals"]
workspace_insights: AsyncIOMotorCollection   = get_client().get_default_database()["workspace_insights"]
workspace_flags: AsyncIOMotorCollection      = get_client().get_default_database()["workspace_flags"]
admin_notifications: AsyncIOMotorCollection  = get_client().get_default_database()["admin_notifications"]
agent_worker_state: AsyncIOMotorCollection   = get_client().get_default_database()["agent_worker_state"]

# ── Collection getter functions ───────────────────────────────────────────────

def get_users_collection() -> AsyncIOMotorCollection:
    return get_db()["users"]

def get_campaigns_collection() -> AsyncIOMotorCollection:
    return get_db()["campaigns"]

def get_brand_profiles_collection() -> AsyncIOMotorCollection:
    return get_db()["brand_profiles"]

def get_jobs_collection() -> AsyncIOMotorCollection:
    return get_db()["jobs"]



def get_text_outputs_collection() -> AsyncIOMotorCollection:
    return get_db()["text_outputs"]

def get_audio_outputs_collection() -> AsyncIOMotorCollection:
    return get_db()["audio_outputs"]

def get_video_outputs_collection() -> AsyncIOMotorCollection:
    return get_db()["video_outputs"]

def get_image_outputs_collection() -> AsyncIOMotorCollection:
    return get_db()["image_outputs"]

def get_content_sessions_collection() -> AsyncIOMotorCollection:
    return get_db()["content_sessions"]

def get_content_pieces_collection() -> AsyncIOMotorCollection:
    return get_db()["content_pieces"]

def get_content_piece_versions_collection() -> AsyncIOMotorCollection:
    return get_db()["content_piece_versions"]

def get_workspace_connections_collection() -> AsyncIOMotorCollection:
    return get_db()["workspace_connections"]


async def create_indexes() -> None:
    """
    Create all MongoDB indexes on startup.
    Safe to call multiple times — idempotent.
    """
    # ── Users ─────────────────────────────────────────────────────────────
    await users.create_index("email", unique=True, sparse=True)
    await users.create_index("phone", unique=True, sparse=True)
    await users.create_index("username", unique=True)
    await users.create_index("auth_identifiers")
    await users.create_index([("id", 1), ("social_accounts.platform", 1)])

    # ── Workspace / tenancy ──────────────────────────────────────────────
    # workspace_id is the primary scoping key across every collection below.
    # user_id is retained on each document as a created_by / audit field.
    await workspaces.create_index("id", unique=True)
    await workspaces.create_index("owner_id")
    await workspace_members.create_index([("workspace_id", 1), ("user_id", 1)], unique=True)
    await workspace_members.create_index("user_id")
    await invites.create_index("token", unique=True)
    await invites.create_index([("workspace_id", 1), ("status", 1)])
    await workspace_connections.create_index([("workspace_id", 1), ("platform", 1)], unique=True)

    # ── Metrics ──────────────────────────────────────────────────────────
    await account_metrics.create_index([("workspace_id", 1), ("platform", 1)], unique=True)
    await post_metrics.create_index(
        [("workspace_id", 1), ("platform", 1), ("platform_post_id", 1)], unique=True
    )
    await post_metrics.create_index([("workspace_id", 1), ("fetched_at", -1)])

    # ── Brand profiles ────────────────────────────────────────────────────
    await brand_profiles.create_index("workspace_id")
    await brand_profiles.create_index("user_id")  # created_by
    await brand_profiles.create_index([("workspace_id", 1), ("is_complete", 1)])
    await brand_profiles.create_index([("workspace_id", 1), ("brand_type", 1)])
    await onboarding_drafts.create_index([("workspace_id", 1), ("user_id", 1)], unique=True)
    await onboarding_drafts.create_index("is_complete")

    # ── Content sessions ──────────────────────────────────────────────────
    await content_sessions.create_index("session_id", unique=True)
    await content_sessions.create_index("workspace_id")
    await content_sessions.create_index("brand_id")
    await content_sessions.create_index([("workspace_id", 1), ("created_at", -1)])
    await content_sessions.create_index([("workspace_id", 1), ("brand_id", 1)])

    # ── Content pieces ────────────────────────────────────────────────────
    await content_pieces.create_index("piece_id", unique=True)
    await content_pieces.create_index("session_id")
    await content_pieces.create_index("workspace_id")
    await content_pieces.create_index([("session_id", 1), ("platform", 1)])
    await content_pieces.create_index([("workspace_id", 1), ("created_at", -1)])
    await content_pieces.create_index([("workspace_id", 1), ("approval_status", 1)])
    await content_pieces.create_index([("workspace_id", 1), ("publish_status", 1)])

    # ── Content piece versions ────────────────────────────────────────────
    await content_piece_versions.create_index("version_id", unique=True)
    await content_piece_versions.create_index("piece_id")
    await content_piece_versions.create_index([("piece_id", 1), ("version_number", 1)])

    publish_incidents = get_client().get_default_database()["publish_incidents"]

    await publish_incidents.create_index("piece_id")
    await publish_incidents.create_index("workspace_id")
    await publish_incidents.create_index([("workspace_id", 1), ("resolved", 1)])
    await publish_incidents.create_index("created_at")

    # ── Two-layer agent architecture ─────────────────────────────────────
    # workspace_events — durable event log; pipeline_type is a first-class,
    # indexed column so a future pipeline's filtered queries stay covered.
    await workspace_events.create_index([("workspace_id", 1), ("occurred_at", -1)])
    await workspace_events.create_index([("workspace_id", 1), ("event_type", 1), ("occurred_at", -1)])
    await workspace_events.create_index([("workspace_id", 1), ("pipeline_type", 1), ("occurred_at", -1)])
    await workspace_events.create_index("idempotency_key", unique=True)
    # 90-day retention — provisional, revisit once event volume is known.
    await workspace_events.create_index("ingested_at", expireAfterSeconds=90 * 24 * 3600)

    # member_personas — one per (workspace_id, user_id); _id is the compound key.
    await member_personas.create_index([("workspace_id", 1), ("user_id", 1)], unique=True)
    await member_personas.create_index("voice.refreshed_at")

    # personal_signals — emitted by Layer 1, read by the member and the supervisor.
    await personal_signals.create_index([("workspace_id", 1), ("user_id", 1), ("created_at", -1)])
    await personal_signals.create_index([("workspace_id", 1), ("created_at", -1)])
    await personal_signals.create_index([("workspace_id", 1), ("severity", 1), ("status", 1)])

    # workspace_insights — Odette's recommendation feed (admin-only reads).
    await workspace_insights.create_index([("workspace_id", 1), ("status", 1), ("priority", 1)])
    await workspace_insights.create_index([("workspace_id", 1), ("created_at", -1)])

    # workspace_flags — rule + LLM flags (admin-only reads).
    await workspace_flags.create_index([("workspace_id", 1), ("status", 1), ("severity", 1)])
    await workspace_flags.create_index([("workspace_id", 1), ("flag_type", 1), ("created_at", -1)])

    # admin_notifications — in-app admin feed.
    await admin_notifications.create_index([("workspace_id", 1), ("created_at", -1)])

    # agent_worker_state — per-workspace checkpoint + debounce bookkeeping.
    await agent_worker_state.create_index("updated_at")

