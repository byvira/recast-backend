"""Activity Log — the Active (decide) / Passive (record) read model.

See ``store`` for the row contract and visibility rules, ``projector`` for how
each source becomes a row, and ``live`` for the SSE fan-out.
"""

from app.shared.activity.projector import (
    project_event,
    project_odette_flag,
    project_odette_insight,
    project_remy_signal,
    record_system,
)

__all__ = [
    "project_event",
    "project_odette_flag",
    "project_odette_insight",
    "project_remy_signal",
    "record_system",
]
