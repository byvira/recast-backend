"""Support tickets: every timing, limit and allow-list in one place.

These are plain constants so they are easy to find and change. Nothing here
reads the environment; change the value and redeploy.
"""

from datetime import timedelta

# ── Lifecycle timing (the scheduler applies these) ───────────────────────────
WAITING_ON_MEMBER_REMINDER_AFTER = timedelta(days=3)
WAITING_ON_MEMBER_AUTO_RESOLVE_AFTER = timedelta(days=7)
RESOLVED_AUTO_CLOSE_AFTER = timedelta(days=7)
AUTO_CLOSE_WARNING_BEFORE = timedelta(days=2)

# ── Limits ───────────────────────────────────────────────────────────────────
MAX_MESSAGE_CHARS = 5000
MAX_TICKETS_PER_MEMBER_PER_HOUR = 5
MAX_MESSAGES_PER_MEMBER_PER_HOUR = 20
MAX_TICKETS_PER_WORKSPACE_PER_DAY = 30
MAX_UPLOADS_PER_MEMBER_PER_HOUR = 40
DUPLICATE_TICKET_WINDOW = timedelta(minutes=10)
MESSAGE_PAGE_SIZE = 50

# ── Attachments ──────────────────────────────────────────────────────────────
MAX_ATTACHMENTS_PER_MESSAGE = 10
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
# A screen recording is bigger than a screenshot, so videos get their own limit.
MAX_VIDEO_BYTES = 50 * 1024 * 1024
VIDEO_EXTENSIONS: frozenset[str] = frozenset({"mp4", "mov", "webm"})
# extension -> mime types accepted for it
ALLOWED_ATTACHMENT_TYPES: dict[str, frozenset[str]] = {
    "png": frozenset({"image/png"}),
    "jpg": frozenset({"image/jpeg"}),
    "jpeg": frozenset({"image/jpeg"}),
    "webp": frozenset({"image/webp"}),
    "pdf": frozenset({"application/pdf"}),
    "txt": frozenset({"text/plain"}),
    "log": frozenset({"text/plain", "application/octet-stream"}),
    "mp4": frozenset({"video/mp4"}),
    "mov": frozenset({"video/quicktime"}),
    "webm": frozenset({"video/webm"}),
}
SIGNED_URL_SECONDS = 300

# ── SLA (first response, resolution) by priority, in hours ───────────────────
# Paid plans get one tier faster: P2 uses the P1 numbers, P3 uses P2's.
SLA_FIRST_RESPONSE_HOURS = {"P1": 1, "P2": 4, "P3": 24}
SLA_RESOLUTION_HOURS = {"P1": 24, "P2": 72, "P3": 168}
SLA_WARN_AT_FRACTION = 0.75
# Billing is not built yet, so nothing is a "paid plan" today. Add tier names
# here (tiers today: single, duo, large) once pricing is decided; the faster
# SLA and the priority bump switch on automatically.
PAID_TIERS: frozenset[str] = frozenset()

# ── AI help ──────────────────────────────────────────────────────────────────
# Off by default: the keyword rules place most tickets, and every AI call spends
# real quota. Turn it on once there is a reason to.
AI_CATEGORY_FALLBACK_ENABLED = False

# ── Privacy ──────────────────────────────────────────────────────────────────
# Closed tickets keep their words for this long, then the text and files are
# removed and only the counts (area, priority, timings, rating) stay.
TICKET_RETENTION = timedelta(days=730)  # about 24 months
RETENTION_BATCH = 200                   # tickets cleaned per housekeeping pass
