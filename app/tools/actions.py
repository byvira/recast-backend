"""Tool definitions for the actions that already exist in the job layer (app.shared.job_actions).

Each action keeps doing its work exactly as before; a tool only describes it: what it is for, what it costs, and how it can be undone.
An action with no definition here fails the coverage test, so a new action cannot be added without deciding how it is offered.
"""

from __future__ import annotations

from typing import Literal

from pydantic import create_model

from app.shared import jobs
from app.tools.contract import Mode, Risk, Scope, ToolSpec

_VERSION_KEPT = "The earlier version stays in the version history."

# name: (summary, risk, price row, price note when there is no single row, undo, plan limits it is held to)
_TABLE: dict[str, tuple[str, Risk, str | None, str, str, tuple[str, ...]]] = {
    "text.generate": (
        "Write posts for one or more platforms from a topic, some text or a link, in the brand's voice.", Risk.WRITE, "text_post",
        "1 credit for each platform (3 for a blog, newsletter or YouTube description)", "", (),
    ),
    "text.regenerate": ("Write a post again with a changed direction.", Risk.WRITE, "text_post", "", _VERSION_KEPT, ()),
    "text.repurpose": (
        "Turn one post into versions for other platforms.", Risk.WRITE, "text_post",
        "1 credit for each platform (3 for long form)", "", (),
    ),
    "text.batch": (
        "Write a batch of days of posts for one topic.", Risk.WRITE, "text_post", "1 credit for each post (3 for long form)", "", (),
    ),
    "audio.transcribe": ("Write out what was said in a recording, with word timing.", Risk.WRITE, "transcription", "", "", ()),
    "audio.cleanup": ("Clean up a recording as a new version.", Risk.WRITE, "cleanup", "", _VERSION_KEPT, ()),
    "audio.assemble": ("Build a finished episode from a recording and the audio kit.", Risk.WRITE, "audio_mix", "", _VERSION_KEPT, ()),
    "audio.dialogue": ("Voice a conversation between several speakers.", Risk.WRITE, "dialogue", "", "", ()),
    "audio.import": (
        "Fetch audio from a link and make a recording from it.", Risk.WRITE, None,
        "Free to import; transcription and any other step are priced when they are run.", "", (),
    ),
    "audio.regenerate": ("Voice a narration again as a new version.", Risk.WRITE, "narration", "", _VERSION_KEPT, ()),
    "audio.video": ("Make a captioned video from a recording.", Risk.WRITE, "render", "", "", ("render_minutes_month",)),
    "audio.director": (
        "Run one goal for a recording: narrate, clean up, chapters, soundbites, show notes and checks.", Risk.WRITE, None,
        "Each step is priced as its own tool, and an estimate is shown first.", "", (),
    ),
    "campaign.next_batch": (
        "Make the next batch of drafts for a campaign.", Risk.WRITE, "campaign_plan",
        "5 credits to plan, then each post and picture at its own price.", "", ("campaigns_active",),
    ),
    "campaign.regenerate_media": ("Make one post's picture again.", Risk.WRITE, "picture", "", "", ("pictures_day",)),
    "campaign.retry_media": (
        "Try again to make a post's picture or recording after a failure.", Risk.WRITE, None,
        "Only what is made is charged, at the normal price of making it.", "", ("pictures_day",),
    ),
    "content.bulk_archive": ("Archive or restore many posts.", Risk.WRITE, None, "Free.", "Restore them with the same tool.", ()),
    "content.bulk_delete": ("Delete many posts.", Risk.DESTRUCTIVE, None, "Free.", "Only support can restore deleted posts.", ()),
    "publish.batch": (
        "Schedule or publish several approved posts.", Risk.PUBLISH, None, "Free.",
        "A post that has not gone out can be cancelled; one that has gone out cannot be taken back by Ekavo.", (),
    ),
    "publish.now": (
        "Publish one approved post now.", Risk.PUBLISH, None, "Free.",
        "Once it has gone out it cannot be taken back by Ekavo.", (),
    ),
    "video.chain": (
        "Write a post for a topic, voice it, make a cover picture, build a video from them and render it.", Risk.WRITE, None,
        "Priced by its steps (post, narration, picture, render), and an estimate is shown first.", "",
        ("render_minutes_month", "pictures_day"),
    ),
    "video.director": (
        "Plan and make a video from one goal.", Risk.WRITE, None,
        "Priced by its steps, and an estimate is shown first.", "", ("render_minutes_month",),
    ),
}


# the routes each action also answers (the same work started from a screen), so the coverage test knows they are offered as this tool
_ROUTES: dict[str, tuple[str, ...]] = {
    "text.generate": ("POST /api/v1/text/generate", "POST /api/v1/text/generate/run"),
    "text.regenerate": ("POST /api/v1/text/regenerate", "POST /api/v1/text/regenerate/run"),
    "text.repurpose": ("POST /api/v1/text/repurpose", "POST /api/v1/text/repurpose/run"),
    "text.batch": ("POST /api/v1/text/batch", "POST /api/v1/text/batch/run"),
    "publish.now": ("POST /api/v1/publish/now",),
    "audio.transcribe": ("POST /api/v1/audio-assets/{audio_asset_id}/transcribe",),
    "audio.cleanup": ("POST /api/v1/audio-assets/{audio_asset_id}/cleanup",),
    "audio.assemble": ("POST /api/v1/audio-assets/{audio_asset_id}/assemble",),
    "audio.video": ("POST /api/v1/audio-assets/{audio_asset_id}/video",),
    "audio.regenerate": ("POST /api/v1/audio-assets/{audio_asset_id}/regenerate",),
    "audio.import": ("POST /api/v1/audio-assets/import",),
    "audio.dialogue": ("POST /api/v1/audio-assets/dialogue",),
    "campaign.next_batch": ("POST /api/v1/campaigns/{campaign_id}/runs", "POST /api/v1/campaigns/{campaign_id}/generate-next-batch"),
    "campaign.regenerate_media": ("POST /api/v1/campaigns/{campaign_id}/pieces/{piece_id}/regenerate-media",),
    "campaign.retry_media": ("POST /api/v1/campaigns/{campaign_id}/pieces/{piece_id}/retry-media",),
}


# fields only a person may set: they override a safety hold or confirm a send, so a key or an assistant that sets one is refused
_PERSON_ONLY: dict[str, tuple[str, ...]] = {
    "publish.now": ("confirm_publish_anyway",),
    "publish.batch": ("confirm_publish_anyway",),
}


def _scope_for(permission: str) -> Scope:
    return Scope.PUBLISH if permission == "publish_content" else Scope.CREATE


def _confirmed(model):
    """The action's input plus an explicit `confirm` that must be true, so a destructive tool cannot run from a vague request."""
    return create_model(f"{model.__name__}Confirmed", __base__=model, confirm=(Literal[True], ...))


def specs() -> list[ToolSpec]:
    if not jobs.ACTIONS:
        from app.shared.job_actions import register_all

        register_all()
    out: list[ToolSpec] = []
    for name, (summary, risk, cost_key, note, undo, limits) in _TABLE.items():
        action = jobs.ACTIONS.get(name)
        if action is None:
            raise LookupError(f"{name}: no such job action")
        model = _confirmed(action.payload_model) if risk is Risk.DESTRUCTIVE else action.payload_model
        out.append(ToolSpec(
            name=name, summary=summary, risk=risk, scope=_scope_for(action.permission), mode=Mode.RUN, input_model=model,
            permission=action.permission, cost_key=cost_key, cost_note=note, limit_keys=limits,
            approval="person" if risk is Risk.PUBLISH else "none", undo=undo, source=f"action:{name}", routes=_ROUTES.get(name, ()),
            human_only_fields=_PERSON_ONLY.get(name, ()),
        ))
    return out
