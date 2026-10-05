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
# Lightweight funnel telemetry — which onboarding step a user reached/completed
# at. Deliberately separate from the governance-events pipeline (workspace_events):
# this is product analytics, not a rule-engine input.
onboarding_funnel_events: AsyncIOMotorCollection = get_client().get_default_database()["onboarding_funnel_events"]
# ── Workspace / tenancy collections ──────────────────────────────────────────
workspaces: AsyncIOMotorCollection = get_client().get_default_database()["workspaces"]
workspace_members: AsyncIOMotorCollection = get_client().get_default_database()["workspace_members"]
invites: AsyncIOMotorCollection = get_client().get_default_database()["invites"]
# Third-party platform tokens, scoped per workspace (replaces users.social_accounts[])
workspace_connections: AsyncIOMotorCollection = get_client().get_default_database()["workspace_connections"]
# Ops Dashboard — admin-entered config for config-driven platforms (webhook /
# manual-handoff / rss_pull), see app/pipelines/publish/platform_config_store.py
platform_configs: AsyncIOMotorCollection = get_client().get_default_database()["platform_configs"]
# What Ops allows for a platform (stage, rollout, live test). One document per registry key; none means the derived defaults.
platform_ops: AsyncIOMotorCollection = get_client().get_default_database()["platform_ops"]
# A workspace's RSS directory listing for a platform: the link they submitted and where it stands. Written by the member.
platform_listings: AsyncIOMotorCollection = get_client().get_default_database()["platform_listings"]
# Content Guard: the single settings document (_id "config") and one row per output it blocked or rewrote.
content_safety: AsyncIOMotorCollection = get_client().get_default_database()["content_safety"]
safety_events: AsyncIOMotorCollection = get_client().get_default_database()["safety_events"]
# One row for each long piece of work that runs in the background (a campaign batch, a recording, a set of pictures): its state, progress and outcome.
pipeline_runs: AsyncIOMotorCollection = get_client().get_default_database()["pipeline_runs"]
# Publish failure audit trail, written by app/pipelines/publish/supervisor/alerts.py
# (previously only ever accessed there via db["publish_incidents"] ad hoc —
# named here too so app/agents/supervisor/rules.py's platform_delivery_failing
# rule can query it the same way every other collection here is queried).
publish_incidents: AsyncIOMotorCollection = get_client().get_default_database()["publish_incidents"]

# ── Remy/Odette scaffolding — real data layer, not yet fed by any real
# pipeline logic (TTS synthesis, cohort scoring, LLM usage metering). See
# app/models/voice_settings.py, lexicon.py, cohort.py, ai_usage.py.
member_voice_settings: AsyncIOMotorCollection = get_client().get_default_database()["member_voice_settings"]
member_lexicon: AsyncIOMotorCollection        = get_client().get_default_database()["member_lexicon"]
workspace_cohorts: AsyncIOMotorCollection     = get_client().get_default_database()["workspace_cohorts"]
workspace_ai_budgets: AsyncIOMotorCollection  = get_client().get_default_database()["workspace_ai_budgets"]
workspace_ai_usage_daily: AsyncIOMotorCollection = get_client().get_default_database()["workspace_ai_usage_daily"]

# The old Ops LLM notes list. Its rows were copied into llm_issues by a startup migration
# (app/shared/llm_health/issues.py::merge_legacy_notes). Nothing writes to it any more.
# REMOVE this line, its index and that migration after one production deploy has run.
ops_llm_notes: AsyncIOMotorCollection = get_client().get_default_database()["ops_llm_notes"]

# Real, workspace-scoped media references — see app.models.media.MediaAsset.
media_assets: AsyncIOMotorCollection = get_client().get_default_database()["media_assets"]

# Support tickets — see app.models.support.SupportTicket.
support_tickets: AsyncIOMotorCollection = get_client().get_default_database()["support_tickets"]
# Append-only audit trail for tickets — see app.shared.support.log_event.
support_ticket_events: AsyncIOMotorCollection = get_client().get_default_database()["support_ticket_events"]
# In-app notifications for tickets, for members and for staff.
support_notifications: AsyncIOMotorCollection = get_client().get_default_database()["support_notifications"]
# Context snapshot staff see beside a ticket — see app.shared.support_context.
support_ticket_context: AsyncIOMotorCollection = get_client().get_default_database()["support_ticket_context"]
# Support attachments (metadata only; the bytes live in private storage).
support_files: AsyncIOMotorCollection = get_client().get_default_database()["support_files"]
# Canned replies staff insert into a reply.
support_canned_replies: AsyncIOMotorCollection = get_client().get_default_database()["support_canned_replies"]
# Who is looking at a ticket right now (expires by itself).
support_presence: AsyncIOMotorCollection = get_client().get_default_database()["support_presence"]
# Admin-editable support settings, e.g. {"_id": "sla", ...}.
support_settings: AsyncIOMotorCollection = get_client().get_default_database()["support_settings"]
# Groups of tickets about the same problem.
support_incidents: AsyncIOMotorCollection = get_client().get_default_database()["support_incidents"]
# Every AI call made for support (drafts, category guesses), for cost and quality tracking.
support_ai_usage: AsyncIOMotorCollection = get_client().get_default_database()["support_ai_usage"]
# One row per email that was attempted and how it ended.
support_email_log: AsyncIOMotorCollection = get_client().get_default_database()["support_email_log"]
# A member's conversations with the support assistant.
support_chats: AsyncIOMotorCollection = get_client().get_default_database()["support_chats"]
# One counter doc ({"_id": "ticket_number", "seq": N}) for human-readable ticket numbers.
support_counters: AsyncIOMotorCollection = get_client().get_default_database()["support_counters"]
# LLM health: failures and notable calls (30 day TTL), hourly counts, per prompt daily counts, grouped issues.
llm_events: AsyncIOMotorCollection = get_client().get_default_database()["llm_events"]
llm_rollups: AsyncIOMotorCollection = get_client().get_default_database()["llm_rollups"]
llm_prompt_daily: AsyncIOMotorCollection = get_client().get_default_database()["llm_prompt_daily"]
llm_issues: AsyncIOMotorCollection = get_client().get_default_database()["llm_issues"]
llm_counters: AsyncIOMotorCollection = get_client().get_default_database()["llm_counters"]
llm_provider_config: AsyncIOMotorCollection = get_client().get_default_database()["llm_provider_config"]
llm_alert_rules: AsyncIOMotorCollection = get_client().get_default_database()["llm_alert_rules"]
llm_alert_log: AsyncIOMotorCollection = get_client().get_default_database()["llm_alert_log"]
# Who changed limits, alert settings, issues or shared a report. Kept, not expired.
llm_audit: AsyncIOMotorCollection = get_client().get_default_database()["llm_audit"]

# One doc per UTC date ({"_id": "2026-09-25", "gemini_calls": N}), app-wide
# (not per-workspace — mirrors Cloudflare's own single shared-account quota).
# Atomically incremented to cap Gemini Nano Banana fallback spend once
# Cloudflare's free tier is exhausted — see
# app.pipelines.media.image_generation._gemini_fallback_slot_available.
image_fallback_usage: AsyncIOMotorCollection = get_client().get_default_database()["image_fallback_usage"]

# ── Sprint 4 — Content storage collections ───────────────────────────────────
content_sessions: AsyncIOMotorCollection = get_client().get_default_database()["content_sessions"]
content_pieces: AsyncIOMotorCollection = get_client().get_default_database()["content_pieces"]
content_piece_versions: AsyncIOMotorCollection = get_client().get_default_database()["content_piece_versions"]
account_metrics: AsyncIOMotorCollection = get_client().get_default_database()["account_metrics"]
post_metrics: AsyncIOMotorCollection    = get_client().get_default_database()["post_metrics"]
presets: AsyncIOMotorCollection          = get_client().get_default_database()["presets"]
# app.pipelines.analytics.snapshots — one row per workspace per day, the
# real baseline for the Home page's week-over-week deltas.
analytics_daily_snapshots: AsyncIOMotorCollection = get_client().get_default_database()["analytics_daily_snapshots"]

# ── Two-layer agent architecture (personal assistant + workspace supervisor) ──
workspace_events: AsyncIOMotorCollection     = get_client().get_default_database()["workspace_events"]
member_personas: AsyncIOMotorCollection      = get_client().get_default_database()["member_personas"]
personal_signals: AsyncIOMotorCollection     = get_client().get_default_database()["personal_signals"]
workspace_insights: AsyncIOMotorCollection   = get_client().get_default_database()["workspace_insights"]
workspace_flags: AsyncIOMotorCollection      = get_client().get_default_database()["workspace_flags"]
admin_notifications: AsyncIOMotorCollection  = get_client().get_default_database()["admin_notifications"]
agent_worker_state: AsyncIOMotorCollection   = get_client().get_default_database()["agent_worker_state"]

# ── Activity Log read model (app.shared.activity) ─────────────────────────────
# One row per thing the Activity Log shows — projected from workspace_events,
# Remy's signals, Odette's insights/flags and system jobs — so the page is one
# indexed query instead of a merge across four collections.
activity_entries: AsyncIOMotorCollection     = get_client().get_default_database()["activity_entries"]

# app.pipelines.analytics.checkpoints — each post's metrics at 1h/24h/72h/7d,
# the fixed-age history post_metrics (latest-only) can't provide.
post_metric_checkpoints: AsyncIOMotorCollection = get_client().get_default_database()["post_metric_checkpoints"]

# app.agents.feedback.trust — shadow-mode trust score per (workspace,
# pipeline, platform), and the per-decision shadow log behind it.
autonomy_trust: AsyncIOMotorCollection  = get_client().get_default_database()["autonomy_trust"]
autonomy_shadow: AsyncIOMotorCollection = get_client().get_default_database()["autonomy_shadow"]

# app.shared.activity.inbox — one read-state doc per (workspace, member).
inbox_state: AsyncIOMotorCollection = get_client().get_default_database()["inbox_state"]

# ── Image pipeline — see app.models.image_asset, pow/audio_image_pipeline/01 ──
image_assets: AsyncIOMotorCollection = get_client().get_default_database()["image_assets"]
image_asset_versions: AsyncIOMotorCollection = get_client().get_default_database()["image_asset_versions"]
image_share_links: AsyncIOMotorCollection = get_client().get_default_database()["image_share_links"]
# Anonymous view/play/listen-through events for the public share page — one
# real row per (token, event_type, day, visitor). Shared across audio and
# image shares since the same public page serves both.
share_view_events: AsyncIOMotorCollection = get_client().get_default_database()["share_view_events"]

# ── Audio pipeline — see app.models.audio_asset, pow/audio_image_pipeline/02 ──
audio_assets: AsyncIOMotorCollection = get_client().get_default_database()["audio_assets"]
audio_asset_versions: AsyncIOMotorCollection = get_client().get_default_database()["audio_asset_versions"]
audio_share_links: AsyncIOMotorCollection = get_client().get_default_database()["audio_share_links"]
audio_comments: AsyncIOMotorCollection = get_client().get_default_database()["audio_comments"]
audio_kits: AsyncIOMotorCollection = get_client().get_default_database()["audio_kits"]
# One real podcast RSS feed per brand — see PodcastFeedSettings
# (app.models.audio_asset). Separate from the generic Show model: a feed
# is scoped by brand_id alone, with no "which episodes belong to this
# show" assignment step for the member to do first.
podcast_feed_settings: AsyncIOMotorCollection = get_client().get_default_database()["podcast_feed_settings"]
shows: AsyncIOMotorCollection = get_client().get_default_database()["shows"]
# Multi-voice dialogue (2026-09-26, bugs/gaps sweep) — see app.models.audio_asset.GuestVoiceProfile
guest_voice_profiles: AsyncIOMotorCollection = get_client().get_default_database()["guest_voice_profiles"]
# Curated CC0 music library, shared across every workspace — see
# app.models.audio_asset.MusicLibraryTrack, app.pipelines.media.music_library
music_library_tracks: AsyncIOMotorCollection = get_client().get_default_database()["music_library_tracks"]
# Real extracted soundbites — the Batch Approval Queue's real backing data,
# see app.models.audio_asset.Soundbite
soundbites: AsyncIOMotorCollection = get_client().get_default_database()["soundbites"]

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


async def _create_llm_health_indexes() -> None:
    """The LLM health log is optional: if its indexes cannot be made, the app still starts."""
    try:
        await llm_events.create_index("at", expireAfterSeconds=30 * 24 * 3600)
        await llm_events.create_index([("issue_id", 1), ("at", -1)])
        await llm_events.create_index([("provider", 1), ("error_type", 1), ("at", -1)])
        await llm_rollups.create_index("hour", expireAfterSeconds=90 * 24 * 3600)
        await llm_prompt_daily.create_index("day", expireAfterSeconds=90 * 24 * 3600)
        await llm_prompt_daily.create_index([("prompt_path", 1), ("day", -1)])
        await llm_issues.create_index("fingerprint", unique=True)
        await llm_issues.create_index([("status", 1), ("last_seen", -1)])
        await llm_issues.create_index("number", unique=True)
        await llm_alert_log.create_index("at", expireAfterSeconds=30 * 24 * 3600)
        await llm_audit.create_index([("at", -1)])
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).error("LLM health indexes not created (the log keeps working without them): %s", exc)


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
    await platform_configs.create_index([("workspace_id", 1), ("platform", 1)], unique=True)
    await platform_ops.create_index("platform_key", unique=True)
    await platform_listings.create_index([("workspace_id", 1), ("platform_key", 1)], unique=True)
    await safety_events.create_index([("created_at", -1)])
    await safety_events.create_index("fingerprint", unique=True)
    await pipeline_runs.create_index("id", unique=True)
    await pipeline_runs.create_index([("workspace_id", 1), ("status", 1), ("created_at", -1)])

    # ── Remy/Odette scaffolding ──────────────────────────────────────────
    await member_voice_settings.create_index([("workspace_id", 1), ("user_id", 1)], unique=True)
    await member_lexicon.create_index([("workspace_id", 1), ("user_id", 1)], unique=True)
    await workspace_cohorts.create_index("workspace_id")
    await workspace_ai_budgets.create_index("workspace_id", unique=True)
    await workspace_ai_usage_daily.create_index([("workspace_id", 1), ("date", 1)], unique=True)
    await ops_llm_notes.create_index([("status", 1), ("created_at", -1)])
    await media_assets.create_index([("workspace_id", 1), ("created_at", -1)])
    await support_tickets.create_index([("workspace_id", 1), ("created_at", -1)])
    await support_tickets.create_index([("status", 1), ("created_at", -1)])
    await support_tickets.create_index([("created_by", 1), ("created_at", -1)])
    await support_tickets.create_index([("status", 1), ("assignee_id", 1)])
    # Tickets filed before numbering existed have no number; only index the ones that do.
    await support_tickets.create_index(
        "number", unique=True, partialFilterExpression={"number": {"$type": "int"}}
    )
    await support_ticket_events.create_index([("ticket_id", 1), ("created_at", 1)])
    await support_notifications.create_index([("user_id", 1), ("audience", 1), ("read", 1), ("created_at", -1)])
    await support_ticket_context.create_index("ticket_id", unique=True)
    await support_files.create_index([("uploaded_by", 1), ("created_at", -1)])
    await support_files.create_index("ticket_id")
    await support_canned_replies.create_index([("owner_id", 1), ("title", 1)])
    await support_presence.create_index("at", expireAfterSeconds=90)
    await support_incidents.create_index([("status", 1), ("created_at", -1)])
    await support_ai_usage.create_index([("created_at", -1)])
    await support_chats.create_index([("user_id", 1), ("workspace_id", 1), ("updated_at", -1)])
    await support_ai_usage.create_index([("staff_id", 1), ("created_at", -1)])
    await support_email_log.create_index("at", expireAfterSeconds=90 * 24 * 3600)
    await _create_llm_health_indexes()
    await support_incidents.create_index([("category", 1), ("platform", 1), ("status", 1)])
    await support_tickets.create_index("incident_id")
    await support_presence.create_index([("ticket_id", 1), ("staff_id", 1)], unique=True)
    await support_tickets.create_index("sla.first_response_due")

    # ── Metrics ──────────────────────────────────────────────────────────
    await account_metrics.create_index([("workspace_id", 1), ("platform", 1)], unique=True)
    await post_metrics.create_index(
        [("workspace_id", 1), ("platform", 1), ("platform_post_id", 1)], unique=True
    )
    await post_metrics.create_index([("workspace_id", 1), ("fetched_at", -1)])
    await analytics_daily_snapshots.create_index([("workspace_id", 1), ("date", 1)], unique=True)

    # ── Brand profiles ────────────────────────────────────────────────────
    await brand_profiles.create_index("workspace_id")
    await brand_profiles.create_index("user_id")  # created_by
    await brand_profiles.create_index([("workspace_id", 1), ("is_complete", 1)])
    await brand_profiles.create_index([("workspace_id", 1), ("brand_type", 1)])
    await onboarding_drafts.create_index([("workspace_id", 1), ("user_id", 1)], unique=True)
    await onboarding_drafts.create_index("is_complete")
    await onboarding_funnel_events.create_index([("workspace_id", 1), ("created_at", -1)])

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
    await content_pieces.create_index("campaign_id")

    # ── Content piece versions ────────────────────────────────────────────
    await content_piece_versions.create_index("version_id", unique=True)
    await content_piece_versions.create_index("piece_id")
    await content_piece_versions.create_index([("piece_id", 1), ("version_number", 1)])

    # ── Presets ──────────────────────────────────────────────────────────
    await presets.create_index("id", unique=True)
    await presets.create_index([("workspace_id", 1), ("deleted", 1), ("updated_at", -1)])
    await presets.create_index([("workspace_id", 1), ("category", 1)])

    # ── Campaigns ────────────────────────────────────────────────────────
    campaigns = get_client().get_default_database()["campaigns"]
    await campaigns.create_index("id", unique=True)
    await campaigns.create_index([("workspace_id", 1), ("deleted", 1), ("updated_at", -1)])
    # app.workers.campaign_scheduler's due-campaign poll.
    await campaigns.create_index([("status", 1), ("cadence.frequency", 1), ("cadence.next_run_at", 1)])

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

    # activity_entries — Activity Log read model. Lane-first so the Active lane
    # (a handful of open agent items) never scans Passive history.
    await activity_entries.create_index([("workspace_id", 1), ("lane", 1), ("occurred_at", -1), ("_id", -1)])
    await activity_entries.create_index([("workspace_id", 1), ("occurred_at", -1), ("_id", -1)])
    # Same 90-day retention as workspace_events. Open Active items carry no
    # expires_at, so an undecided suggestion never silently disappears.
    await activity_entries.create_index("expires_at", expireAfterSeconds=0)

    # post_metric_checkpoints — learning reads are per member / per workspace
    # at one checkpoint age.
    await post_metric_checkpoints.create_index([("workspace_id", 1), ("checkpoint", 1), ("captured_at", -1)])
    await post_metric_checkpoints.create_index([("workspace_id", 1), ("user_id", 1), ("checkpoint", 1)])
    # content_pieces — the checkpoint job's "recently published" scan.
    await content_pieces.create_index([("workspace_id", 1), ("publish_status", 1), ("published_at", -1)])
    # Trust score aggregation — one tuple's drafts in the 60-day window.
    await content_pieces.create_index([("workspace_id", 1), ("platform", 1), ("created_at", -1)])
    await autonomy_trust.create_index("workspace_id")
    await autonomy_shadow.create_index([("workspace_id", 1), ("platform", 1), ("recorded_at", -1)])
    # The shadow log is evidence for a decision, not an audit trail — 180 days.
    await autonomy_shadow.create_index("recorded_at", expireAfterSeconds=180 * 24 * 3600)

    # localized_strings — runtime-translation cache (app.shared.localized_strings).
    # One doc per (key, language); language is an opaque string, not validated
    # against any fixed set.
    localized_strings = get_client().get_default_database()["localized_strings"]
    await localized_strings.create_index([("key", 1), ("language", 1)], unique=True)

    # ── Image pipeline ────────────────────────────────────────────────────
    await image_assets.create_index([("workspace_id", 1), ("created_at", -1)])
    await image_assets.create_index([("workspace_id", 1), ("approval_status", 1)])
    # Stale-upstream detection (Phase 3) needs a fast lookup of every
    # ImageAsset derived from a given text piece.
    await image_assets.create_index("source_piece_id")
    await image_asset_versions.create_index(
        [("image_asset_id", 1), ("version_number", -1)]
    )
    await image_share_links.create_index("token", unique=True)
    await image_share_links.create_index([("image_asset_id", 1), ("revoked", 1)])

    # ── Audio pipeline ────────────────────────────────────────────────────
    await audio_assets.create_index([("workspace_id", 1), ("created_at", -1)])
    await audio_assets.create_index([("workspace_id", 1), ("approval_status", 1)])
    await audio_assets.create_index("source_piece_id")
    await audio_assets.create_index([("show_id", 1), ("created_at", 1)])
    await audio_asset_versions.create_index(
        [("audio_asset_id", 1), ("version_number", -1)]
    )
    await audio_share_links.create_index("token", unique=True)
    await audio_share_links.create_index([("audio_asset_id", 1), ("revoked", 1)])
    await audio_comments.create_index([("audio_asset_id", 1), ("time_s", 1)])
    # A duplicate insert for the same (token, event_type, day, visitor) is a
    # real re-view/re-play, not a new one to count — this index is what
    # makes that "insert, ignore the duplicate" dedup real rather than best-effort.
    await share_view_events.create_index(
        [("token", 1), ("event_type", 1), ("day", 1), ("visitor_hash", 1)], unique=True,
    )
    await share_view_events.create_index("token")
    await audio_kits.create_index([("workspace_id", 1), ("brand_id", 1)], unique=True)
    await podcast_feed_settings.create_index("brand_id", unique=True)
    await podcast_feed_settings.create_index("token", unique=True, sparse=True)
    await shows.create_index([("workspace_id", 1), ("created_at", -1)])
    await guest_voice_profiles.create_index([("audio_asset_id", 1)])
    await music_library_tracks.create_index("source_track_id", unique=True)
    await soundbites.create_index([("audio_asset_id", 1), ("created_at", 1)])
    await soundbites.create_index([("workspace_id", 1), ("approval_status", 1)])

