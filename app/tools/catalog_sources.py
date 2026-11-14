"""Tools for Sources: feeds and pages that Ekavo checks and turns into drafts.

A source only ever makes drafts, as the person who set it up, so none of these tools publishes. Checking an address fetches it once and
keeps nothing, so it is a change-scope tool (a read-only key cannot make Ekavo fetch an address)."""

from __future__ import annotations

from app.tools.catalog import T, build
from app.tools.contract import Risk, ToolSpec

S = "/api/v1/sources"
_ID = f"{S}/{{source_id}}"
_RUN_COST = "Following a feed is free. A draft that only saves the link is free; a written draft is priced like writing a post, and the price is shown first."


def rows() -> list[T]:
    return [
        T("sources.list", "List the feeds and pages being followed, with their status and the limits.", Risk.READ, (f"GET {S}",),
          examples=("Which feeds am I following?",)),
        T("sources.get", "Show one followed feed or page: its settings, recent items and how many of its drafts were kept.", Risk.READ, (f"GET {_ID}",),
          examples=("How is my industry news feed doing?",)),
        T("sources.check", "Fetch an address once and show what its first items would be. Nothing is followed or made.", Risk.WRITE, (f"POST {S}/check",),
          note="Free. Nothing is kept.", examples=("Check this feed before I follow it",)),
        T("sources.create", "Follow a feed or a page, so new items become drafts for you to look at.", Risk.WRITE, (f"POST {S}",),
          note=_RUN_COST, undo="Stop following it with sources.remove. Drafts it already made are kept.",
          examples=("Follow this blog and draft the good items",)),
        T("sources.update", "Change what a followed source does: its name, platforms, mode, relevance, daily limit or how often it is checked.", Risk.WRITE,
          (f"PATCH {_ID}",), note="Free.", undo="Change it back with the same tool."),
        T("sources.pause", "Stop a source from making drafts for now.", Risk.WRITE, (f"POST {_ID}/pause",), note="Free.", undo="Turn it back on with sources.resume."),
        T("sources.resume", "Start a paused source again.", Risk.WRITE, (f"POST {_ID}/resume",), note="Free.", undo="Pause it again with sources.pause."),
        T("sources.run", "Check a source now instead of waiting. Makes drafts only, and only the person who set it up can run it.", Risk.WRITE,
          (f"POST {_ID}/run",), note=_RUN_COST, examples=("Check that feed now",)),
        T("sources.rate_item", "Say that one of a source's items was a good one, or not for you. It only helps the results; no setting changes.", Risk.WRITE,
          (f"POST {_ID}/items/{{item_id}}/feedback",), note="Free.", undo="Rate it again, or clear it, with the same tool.", examples=("That last item was a good one",)),
        T("sources.remove", "Stop following a source. Drafts it already made are kept.", Risk.DESTRUCTIVE, (f"DELETE {_ID}",),
          undo="Only support can restore it; or follow it again with sources.create. Drafts it made are kept.", examples=("Stop following that feed",)),
    ]


def specs() -> list[ToolSpec]:
    return build(rows())
