"""How well each group of people keeps to the brand, scored from what their posts went through.

Three plain measures, each a share of the group's posts over the window, and shown only when there are enough posts to mean something:

* Passed the quality checks: the post cleared the writing checks without leaving a problem to fix.
* Never held back: the safety checks did not hold the post for a person to look at.
* Approved when decided: of the posts a person has approved or turned down, how many were approved.

Nothing here guesses. A measure with too few posts to judge is None, so a group is never scored on two posts."""
from __future__ import annotations

from typing import Any, Iterable, Optional

#: The least posts a measure needs before it is shown.
MIN_POSTS = 3

AXES: list[tuple[str, str]] = [
    ("quality", "Passed the quality checks"),
    ("safety", "Never held back"),
    ("approval", "Approved when decided"),
]


def _share(hits: int, total: int) -> Optional[int]:
    return round(100 * hits / total) if total >= MIN_POSTS else None


def score_group(posts: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """The group's three measures, as whole percentages (None when there are too few posts to judge)."""
    rows = list(posts)
    decided = [p for p in rows if p.get("approval_status") in ("approved", "rejected")]
    percent = {
        "quality": _share(sum(1 for p in rows if p.get("quality_passed", True) is not False), len(rows)),
        "safety": _share(sum(1 for p in rows if not p.get("flagged_for_review")), len(rows)),
        "approval": _share(sum(1 for p in decided if p.get("approval_status") == "approved"), len(decided)),
    }
    return {
        "posts": len(rows),
        "axes": [{"key": key, "label": label, "percent": percent[key]} for key, label in AXES],
    }
