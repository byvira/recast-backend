"""Every tunable the workspace supervisor uses, in one place.

ALL VALUES ARE LAUNCH DEFAULTS, NOT FINAL. They were picked from first
principles with no usage data. Expect to revisit each once real workspaces are
running — how chatty the rule flags are, whether the 20-event batch trigger
fires too often/rarely, whether the publish caps match real tiers, etc.
"""

# ── Tier daily publish caps (rule: daily_publish_cap) ──────────────────────
# Keyed by the workspace's `tier` string. Provisional — align with real plans.
DAILY_PUBLISH_CAP: dict[str, int] = {
    "single": 10,
    "duo": 30,
    "large": 150,
}
DEFAULT_PUBLISH_CAP = 30          # provisional: fallback for an unknown tier

# ── Rule windows / thresholds ─────────────────────────────────────────────
RULE_LOOKBACK_HOURS = 24         # provisional: window most 24h rules evaluate over
BRAND_VOICE_EDITS_24H = 3        # provisional: >= this many brand.voice_updated in 24h → instability
MEMBER_REMOVALS_24H = 2          # provisional: >= this many member.removed in 24h → churn
SIGNAL_STORM_PER_MEMBER_24H = 5  # provisional: >= this many personal signals from one member in 24h
SIGNAL_STORM_SHARED_TYPE_MEMBERS = 3   # provisional: same signal_type from >= this many members...
SIGNAL_STORM_SHARED_TYPE_HOURS = 6     # provisional: ...within this window → storm
ELEVATED_ROLES = ("owner", "admin")    # role.changed into one of these → churn/govern flag

# ── LLM reasoning-pass debounce / batch trigger (reason_tick) ─────────────
REASON_TICK_SECONDS = 300        # provisional: cron cadence for the reasoning pass
BATCH_EVENT_COUNT = 20           # provisional: events buffered since last pass → run now
BATCH_HEARTBEAT_S = 900          # provisional: force a pass this long after the last one...
BATCH_HEARTBEAT_MIN_EVENTS = 3   # provisional: ...if at least this many events are pending
LLM_PASS_LOCK_SECONDS = 240      # provisional: coalescing lock while a pass runs
EVENT_BUFFER_CAP = 200           # provisional: max buffered event summaries per workspace

# ── ReAct tool loop ──────────────────────────────────────────────────────
MAX_TOOL_CALLS = 6               # provisional: hard cap on investigative tool calls per pass
TOOL_RESULT_CHAR_CAP = 4000      # provisional: truncate each tool result fed back to the model
REASON_MAX_TOKENS = 1400         # provisional
SYNTH_MAX_TOKENS = 1600          # provisional

# ── volume_drop periodic sweep (moved off the per-piece personal graph) ──
VOLUME_DROP_SWEEP_HOURS = 6      # provisional: how often the sweep runs
VOLUME_DROP_DEDUP_HOURS = 24     # provisional: don't re-raise volume_drop for a member within this window
