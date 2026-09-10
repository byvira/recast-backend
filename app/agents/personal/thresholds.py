"""Every tunable number the personal assistant uses, in one place.

ALL VALUES HERE ARE LAUNCH DEFAULTS, NOT FINAL. They were chosen from first
principles, not from data. Expect to revisit every one of them once there is
real usage to calibrate against — drift false-positive rate, how noisy the
volume signal is on small teams, whether the topic-shift Jaccard cutoff is
too twitchy, etc. Treat this module as the single knob-board for that tuning.
"""

# ── Voice drift ────────────────────────────────────────────────────────────
# A single global cosine cutoff does NOT separate "same author, different topic"
# from "genuinely off-voice" for the embedding model in use — those overlap
# around 0.75-0.86. So drift is scored two ways and the stronger wins:
#   1. an ADAPTIVE embedding score: how many stddevs below the member's OWN
#      running mean cosine-to-baseline this piece sits (needs a few pieces of
#      history first; before that the embedding score is ignored entirely).
#   2. a STYLE-divergence score from the deterministic fingerprint (reading
#      grade, sentence length, habit flips) — this is what actually catches a
#      register shift (casual → corporate) that embeddings miss.
# Combined drift_score: <1 in-voice, 1-2 soft (templated, no LLM), >=2 judge (LLM).
SIM_IN_VOICE = 0.86          # provisional: absolute-cosine backstop, used only before a personal norm exists
SIM_SOFT_FLOOR = 0.78        # provisional: absolute-cosine backstop floor
SIM_HARD_DRIFT = 0.65        # provisional: below this cosine, force >= soft drift regardless of everything else
EMB_Z_MIN_STD = 0.02         # provisional: floor on the per-member cosine stddev (guards tiny samples)
EMB_Z_FULL_SIGMA = 3.0       # provisional: this many stddevs below the member's norm == full embedding drift
EMB_Z_MIN_SAMPLES = 5        # provisional: real per-piece cosines needed before the adaptive score engages
STYLE_DIV_SOFT = 0.6         # provisional: style-divergence score that counts as one unit of drift

# ── Voice drift trend (rolling average, catches slow drift that never trips
#    a single-piece threshold) ────────────────────────────────────────────────
TREND_RECENT_N = 5           # provisional: size of the recent window
TREND_BASELINE_N = 20        # provisional: size of the baseline window
TREND_DROP = 0.05            # provisional: recent-avg below baseline-avg by more → trend signal

# ── Baseline maintenance ────────────────────────────────────────────────────
BASELINE_MAX_SAMPLES = 20    # provisional: pieces the EWMA centroid is built from
BASELINE_REFRESH_EVERY = 5   # provisional: recompute the centroid every N pieces
#                              (and always while fewer than BASELINE_MAX_SAMPLES exist)
RECENT_SIM_KEEP = 20         # provisional: how many per-piece cosines to retain

# ── Unusual output volume ───────────────────────────────────────────────────
VOLUME_WINDOW_DAYS = 14      # provisional: trailing window for the daily mean/stddev
VOLUME_SPIKE_SIGMA = 3.0     # provisional: today's count > mean + SIGMA*stddev → spike
VOLUME_SPIKE_MIN_COUNT = 5   # provisional: ...and at least this many, to mute tiny-baseline noise
VOLUME_DROP_BASELINE_PER_DAY = 3.0   # provisional: a member averaging >= this over the window...
VOLUME_DROP_ZERO_DAYS = 3    # provisional: ...who then posts nothing for this many days → drop

# ── Topic shift (Jaccard overlap of keyword sets) ──────────────────────────
TOPIC_RECENT_N = 5           # provisional: keyword set from the last N pieces
TOPIC_BASELINE_N = 30        # provisional: keyword set from the last N pieces
TOPIC_JACCARD_FLOOR = 0.2    # provisional: overlap below this → topic-shift signal
TOPIC_KEYWORDS_PER_PIECE = 8 # provisional: top-K keywords kept per piece

# ── Quality regression (uses the piece's own quality_passed / flagged flags) ─
QUALITY_TRAILING_N = 10      # provisional: window for the recent flag rate
QUALITY_FLAG_RATE_TRIGGER = 0.40   # provisional: recent flagged rate >= this...
QUALITY_BASELINE_FLAG_RATE_MAX = 0.15   # provisional: ...while lifetime baseline is below this → signal

# ── Assist / nudge ─────────────────────────────────────────────────────────
NUDGE_CACHE_TIMEOUT_S = 0.15   # provisional: hard cap on the cached persona read wired into pipeline responses
ASSIST_MIN_PIECES = 3         # provisional: fewer observed pieces than this → assist says "still learning"

# ── Drift history ──────────────────────────────────────────────────────────
DRIFT_HISTORY_CAP = 100      # provisional: entries retained on the persona doc
