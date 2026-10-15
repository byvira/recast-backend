"""Media for a campaign's posts: the audio and images a member asked for on the campaign.

Runs after a day's text is saved. It never raises: media is an extra on top of the text, so a
failed narration or image is logged and counted, and the text and every other post are kept.
Each asset carries `source_piece_id`, which is how a post is tied to its media.

Audio is one narration per post (the same script voiced again would be an identical file);
`count_per_post` is how many images each post gets. Video is the post voiced over its own picture: an editable project is built
from the narration and the picture, rendered, and the finished video goes onto the post.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from app.core.workspace import WorkspaceContext
from app.pipelines.media import duration
from app.db.mongo import audio_assets, content_pieces, image_assets, users, workspace_members, workspaces
from app.pipelines.publish.attachments import attach_generated_image

logger = logging.getLogger(__name__)

GENERATED_KINDS = ("image", "audio", "video")
#: How many posts one run makes media for, and how many are worked on at the same time. Each post's pictures, narration and
#: video wait on other services, so a few at once make a run much shorter without hitting a service's rate limit.
MAX_POSTS_WITH_MEDIA_PER_RUN = 60
POSTS_AT_ONCE = 3
#: How long a campaign waits for one video to be drawn before it is marked failed (it can be tried again).
VIDEO_WAIT_SECONDS = 20 * 60
_ASPECT_TO_SIZE = {"9:16": "vertical", "16:9": "landscape", "1:1": "square"}


class _NoRun:
    async def step(self, label: str) -> None:
        return None


def wanted_kinds(media_plan: dict | None) -> list[str]:
    """The kinds that will really be made for this plan (empty when media is off)."""
    plan = media_plan or {}
    if not plan.get("enabled"):
        return []
    return [k for k in GENERATED_KINDS if k in (plan.get("kinds") or [])]


def estimate(media_plan: dict | None, posts: int) -> dict[str, int]:
    """How many files a run of `posts` posts will make, so the member can see it before starting."""
    plan = media_plan or {}
    kinds = wanted_kinds(plan)
    return {
        "images": posts * int(plan.get("count_per_post", 1)) if "image" in kinds else 0,
        "audio": posts if "audio" in kinds else 0,
        "videos": posts if "video" in kinds else 0,
    }


def _headline(content: str) -> str:
    for line in (content or "").splitlines():
        line = line.strip().lstrip("#*- ").strip()
        if line:
            return line[:90]
    return "Post"


async def _context(workspace_id: str, user_id: str) -> WorkspaceContext:
    workspace = await workspaces.find_one({"id": workspace_id}) or {"id": workspace_id}
    member = await workspace_members.find_one({"workspace_id": workspace_id, "user_id": user_id})
    if not member or member.get("status", "active") != "active":
        # No invented owner: work that is not done by a current member does not run with a member's rights.
        raise PermissionError("The person who started this is no longer an active member of the workspace.")
    user = await users.find_one({"id": user_id}) or {"id": user_id}
    return WorkspaceContext(workspace, member, user)


async def _image_request(campaign: dict[str, Any], piece: dict):
    from app.api.v1.image_assets import GenerateImageAssetRequest
    from app.pipelines.media.headline import make_headline

    plan = campaign.get("media_plan") or {}
    count = int(plan.get("count_per_post", 1))
    image_plan = plan.get("image") or {}
    layout = image_plan.get("layout") or "quote_1_1"
    content = (piece.get("content") or "").strip()
    title = _headline(content)  # the asset's name in the library
    show_text = image_plan.get("text", "headline") != "none"
    # The headline is written in the language the post was written in (it used to always be English).
    from app.pipelines.text.generator import resolve_language_directive_name

    language = (piece.get("language") or "").strip()
    language_name = resolve_language_directive_name(language) if language and language != "en" else "English"
    headline = (await make_headline(content, language_name=language_name)) if show_text else ""
    return GenerateImageAssetRequest(
        title=title, brand_id=campaign["brand_id"], headline=headline, show_text=show_text,
        show_logo=image_plan.get("logo", True), show_mascot=bool(image_plan.get("mascot", False)),
        source_piece_id=piece["piece_id"], count=min(count, 5), active_layout=layout,
        visual_profile=image_plan.get("visual_profile") or "midnight_swiss",
    )


def _kinds_this_platform_takes(piece: dict, kinds: list[str]) -> tuple[list[str], dict[str, str]]:
    """Splits `kinds` into the ones the post's platform can carry and the ones it cannot (marked "skipped", so no picture,
    recording or video is made that could never be attached). A platform the registry does not know keeps every kind."""
    from app.platforms.base import PLATFORM_REGISTRY, import_all
    from app.pipelines.publish.spine import platform_key

    import_all()
    definition = PLATFORM_REGISTRY.get(platform_key(piece.get("platform")))
    if definition is None:
        return kinds, {}
    usable = [k for k in kinds if definition.modality(k) != "none"]  # type: ignore[arg-type]
    return usable, {k: "skipped" for k in kinds if k not in usable}


async def _narration(
    campaign: dict[str, Any], piece: dict, ctx: WorkspaceContext, title: str, content: str,
) -> Optional[str]:
    """The recording of this post's words, made once: a re-run for the same post and the same words reuses the recording already
    made instead of voicing it again (which spends provider quota and leaves a second identical recording). Returns its file id."""
    from app.api.v1.audio_assets import GenerateAudioAssetRequest, create_audio_from_script

    audio_opts = (campaign.get("media_plan") or {}).get("audio") or {}
    wpm = audio_opts.get("words_per_minute")
    script = content
    if audio_opts.get("max_seconds"):
        script = duration.trim_to_seconds(content, audio_opts["max_seconds"], wpm)
    already = await audio_assets.find_one(
        {"workspace_id": ctx.workspace_id, "campaign_id": campaign["id"], "source_piece_id": piece["piece_id"], "script": script},
        {"id": 1, "media_id": 1},
    )
    if already:
        return already.get("media_id")
    made = await create_audio_from_script(
        GenerateAudioAssetRequest(
            title=title, brand_id=campaign["brand_id"], source_piece_id=piece["piece_id"],
            script=script, words_per_minute=wpm, language=piece.get("language"),
            delivery_style=audio_opts.get("delivery_style"), mastering=audio_opts.get("mastering"),
        ),
        ctx,
        _NoRun(),
    )
    await audio_assets.update_one({"id": made.id, "workspace_id": ctx.workspace_id}, {"$set": {"campaign_id": campaign["id"]}})
    return getattr(made, "media_id", None)


async def _make_video(
    campaign: dict[str, Any], piece: dict, ctx: WorkspaceContext, title: str, content: str,
    voice_media_id: Optional[str], picture_media_id: Optional[str],
) -> str:
    """The post voiced over its picture: an editable project, rendered, and attached to the post. Returns the project id."""
    from app.agents.chain.topic_to_video import build_narrated_project
    from app.db.mongo import get_db
    from app.pipelines.publish import attachments
    from app.video import service

    if not voice_media_id:
        voice_media_id = await _narration(campaign, piece, ctx, title, content)
    if not voice_media_id:
        raise RuntimeError("There is no narration to build a video from.")
    options = (campaign.get("media_plan") or {}).get("video") or {}
    size = _ASPECT_TO_SIZE.get(options.get("aspect", "9:16"), "vertical")
    project_id, _ = await build_narrated_project(
        ctx, title=title, text=content, voice_media_id=voice_media_id, cover_media_id=picture_media_id, brand_id=campaign.get("brand_id"),
        size=size, parents=[{"kind": "piece", "id": piece["piece_id"]}],
        created_from={"kind": "campaign", "campaign_id": campaign["id"], "piece_id": piece["piece_id"]},
    )
    await get_db()["video_projects"].update_one({"id": project_id, "workspace_id": ctx.workspace_id}, {"$set": {"campaign_id": campaign["id"]}})
    await service.render_project_and_wait(
        workspace_id=ctx.workspace_id, user_id=ctx.user_id, project_id=project_id, preset="standard", max_wait_s=VIDEO_WAIT_SECONDS,
    )
    fresh = await content_pieces.find_one({"piece_id": piece["piece_id"], "workspace_id": ctx.workspace_id})
    if fresh:
        resolved = await attachments.resolve_asset(ctx.workspace_id, "project", asset_id=project_id)
        await attachments.attach(fresh, ctx.workspace_id, ctx.user_id, resolved)
    return project_id


async def _make_for_piece(
    campaign: dict[str, Any], piece: dict, kinds: list[str], ctx: WorkspaceContext,
) -> dict[str, str]:
    """Make the planned media for one post. Returns {kind: "ready" | "failed"} for each kind tried."""
    # imported here: these modules import the whole API layer
    from app.api.v1.image_assets import create_image_asset

    content = (piece.get("content") or "").strip()
    title = _headline(content)
    kinds, states = _kinds_this_platform_takes(piece, kinds)
    picture_media_id: Optional[str] = None
    if "image" in kinds or "video" in kinds:
        # a video is built over the post's picture, so the picture is made first (and is a result of its own when it was asked for)
        try:
            made = await create_image_asset(await _image_request(campaign, piece), ctx)
            if "image" in kinds:
                states["image"] = "ready"
            slides = getattr(made, "slides", None) or []
            picture_media_id = next((getattr(s, "media_id", None) for s in slides if getattr(s, "media_id", None)), None)
            await image_assets.update_one({"id": getattr(made, "id", ""), "workspace_id": ctx.workspace_id}, {"$set": {"campaign_id": campaign["id"]}})
            if "image" in kinds:
                # The picture goes onto its post, so it is what gets published.
                await attach_generated_image(piece["piece_id"], ctx.workspace_id, ctx.user_id, getattr(made, "id", ""))
        except Exception as exc:  # noqa: BLE001 - media must never fail the text
            if "image" in kinds:
                states["image"] = "failed"
            logger.warning("Campaign %s: image for piece %s failed: %s", campaign["id"], piece["piece_id"], exc)
    voice_media_id: Optional[str] = None
    if "audio" in kinds:
        try:
            voice_media_id = await _narration(campaign, piece, ctx, title, content)
            states["audio"] = "ready"
        except Exception as exc:  # noqa: BLE001
            states["audio"] = "failed"
            logger.warning("Campaign %s: audio for piece %s failed: %s", campaign["id"], piece["piece_id"], exc)
    if "video" in kinds:
        try:
            await _make_video(campaign, piece, ctx, title, content, voice_media_id, picture_media_id)
            states["video"] = "ready"
        except Exception as exc:  # noqa: BLE001 - the text, the picture and the narration are kept
            states["video"] = "failed"
            logger.warning("Campaign %s: video for piece %s failed: %s", campaign["id"], piece["piece_id"], exc)
    return states


async def _record_status(workspace_id: str, piece_id: str, states: dict[str, str]) -> None:
    """Saved on the post so the stage matrix can show ready or failed for each kind."""
    if not states:
        return
    await content_pieces.update_one(
        {"workspace_id": workspace_id, "piece_id": piece_id},
        {"$set": {f"media_status.{kind}": state for kind, state in states.items()}},
    )


async def generate_media_for_pieces(
    campaign: dict[str, Any], piece_ids: list[str], *, workspace_id: str, user_id: str, run_id: Optional[str] = None,
) -> dict[str, int]:
    """Make the planned media for each of `piece_ids`, a few posts at a time. Returns counts of what was made and what failed.
    A post that fails does not stop the others, and a pause or cancel is honoured between posts."""
    kinds = wanted_kinds(campaign.get("media_plan"))
    outcome = {"images": 0, "audio": 0, "videos": 0, "failed": 0}
    if not kinds or not piece_ids:
        return outcome

    ctx = await _context(workspace_id, user_id)
    count = int((campaign.get("media_plan") or {}).get("count_per_post", 1))
    pieces = await content_pieces.find(
        {"workspace_id": workspace_id, "piece_id": {"$in": piece_ids}, "deleted": {"$ne": True}},
    ).to_list(length=None)

    batch = [p for p in pieces[:MAX_POSTS_WITH_MEDIA_PER_RUN] if (p.get("content") or "").strip()]
    gate = asyncio.Semaphore(POSTS_AT_ONCE)
    started = 0
    finished = 0

    async def one(piece: dict) -> None:
        nonlocal started, finished
        async with gate:
            if run_id:
                from app.shared import pipeline_runs

                await pipeline_runs.checkpoint(run_id)
                started += 1
                what = " and ".join(w for w, k in (("pictures", "image"), ("audio", "audio"), ("videos", "video")) if k in kinds) or "media"
                await pipeline_runs.set_stage(
                    run_id, f"Making {what}: post {started} of {len(batch)}", media_done=finished, media_total=len(batch),
                )
            states = await _make_for_piece(campaign, piece, kinds, ctx)
            await _record_status(workspace_id, piece["piece_id"], states)
            outcome["images"] += count if states.get("image") == "ready" else 0
            outcome["audio"] += 1 if states.get("audio") == "ready" else 0
            outcome["videos"] += 1 if states.get("video") == "ready" else 0
            outcome["failed"] += sum(1 for v in states.values() if v == "failed")
            finished += 1

    results = await asyncio.gather(*[one(p) for p in batch], return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException):
            # a pause or a cancel ends the run where it stands; anything else is one post's failure and is counted
            if type(r).__name__ in ("RunCancelled", "RunPaused", "CancelledError"):
                raise r
            outcome["failed"] += 1
            logger.warning("Campaign %s: media for a post stopped: %s", campaign["id"], r)
    return outcome


async def attach_source_cut(
    campaign: dict[str, Any], piece_ids: list[str], day_index: int, days: int, *, workspace_id: str, user_id: str,
) -> Optional[str]:
    """For a campaign that starts from a video the member already has: this day's part of the video, cut as an editable project
    with its own words as captions, rendered, and attached to each of the day's posts that can carry a video. Nothing is generated.
    Returns the project id, or None when the day has nothing to cut."""
    from app.db.mongo import get_db, media_assets
    from app.pipelines.campaigns import sources
    from app.pipelines.publish import attachments
    from app.video import edl, service, transcribe
    from app.video.flows import registry as R
    from app.video.flows import runner

    ref = campaign.get("source_ref") or {}
    media = await media_assets.find_one({"id": ref.get("id"), "workspace_id": workspace_id, "kind": "video"})
    if not media:
        return None
    start_s, length_s = sources.window_for_day(float(media.get("duration_s") or ref.get("duration_s") or 0), day_index, days)
    if length_s < 1:
        return None
    options = (campaign.get("media_plan") or {}).get("video") or {}
    size = _ASPECT_TO_SIZE.get(options.get("aspect", "9:16"), "vertical")
    fps = {"num": 30, "den": 1}
    dur = max(1, edl.seconds_to_frames(length_s, fps))
    edit = edl.blank(size, fps)
    edit["video"] = [{"id": "c1", "media_id": media["id"], "at_f": 0, "dur_f": dur, "src_in_f": edl.seconds_to_frames(start_s, fps), "speed": 1.0,
                      "fit": "cover", "fade_in_f": 0, "fade_out_f": 0, "audio": True, "gain_db": 0.0, "motion": False}]
    transcript = await get_db()["video_transcripts"].find_one({"workspace_id": workspace_id, "media_id": media["id"]})
    words: list[dict[str, Any]] = []
    if transcript:
        lo, hi = int(start_s * 1000), int((start_s + length_s) * 1000)
        words = [{"w": w["w"], "s_ms": w["s_ms"] - lo, "e_ms": min(w["e_ms"], hi) - lo}
                 for w in await transcribe.all_words(transcript["id"]) if lo <= w["s_ms"] < hi]
    edit["captions"] = {**edit["captions"], "enabled": bool(words), "words": words}
    edit["duration_f"] = dur
    edit = edl.validate(edit)
    env = R.Env(workspace_id=workspace_id, user_id=user_id, reporter=None)
    title = f"{campaign.get('name') or ref.get('title') or 'Video'}, day {day_index + 1}"
    made = await runner.create_projects(env, [{
        "title": title, "edit": edit, "notes": [],
        "created_from": {"kind": "campaign", "campaign_id": campaign["id"], "source_media_id": media["id"], "day": day_index + 1,
                         "derived_from": [{"kind": "video", "id": media["id"]}]},
    }])
    project_id = made[0]["project_id"]
    await get_db()["video_projects"].update_one({"id": project_id, "workspace_id": workspace_id}, {"$set": {"campaign_id": campaign["id"]}})
    await service.render_project_and_wait(workspace_id=workspace_id, user_id=user_id, project_id=project_id, preset="standard",
                                          max_wait_s=VIDEO_WAIT_SECONDS)
    pieces = await content_pieces.find({"workspace_id": workspace_id, "piece_id": {"$in": piece_ids}}).to_list(length=None)
    for piece in pieces:
        usable, _ = _kinds_this_platform_takes(piece, ["video"])
        if not usable:
            continue
        resolved = await attachments.resolve_asset(workspace_id, "project", asset_id=project_id)
        await attachments.attach(piece, workspace_id, user_id, resolved)
    return project_id


async def regenerate_image_for_piece(
    campaign: dict[str, Any], piece_id: str, *, workspace_id: str, user_id: str,
) -> dict[str, Any]:
    """Make this post's picture again, even though one exists. The new picture replaces the old one on the
    post; the old one is kept in the media library, marked as replaced. A new card with no AI picture never
    replaces a real picture: the old one stays and the reason is returned.

    Returns {"state": "missing" | "failed" | "kept" | "card" | "ready", "note": str | None}."""
    from app.api.v1.image_assets import create_image_asset
    from app.db.mongo import image_assets, media_assets

    piece = await content_pieces.find_one({"workspace_id": workspace_id, "piece_id": piece_id, "deleted": {"$ne": True}})
    if not piece or piece.get("campaign_id") != campaign["id"] or not (piece.get("content") or "").strip():
        return {"state": "missing", "note": None}
    if "image" not in wanted_kinds(campaign.get("media_plan")):
        return {"state": "missing", "note": "This campaign is not set up to make pictures."}
    if piece_id in _REGENERATING:
        return {"state": "failed", "note": "A new picture for this post is already being made."}
    _REGENERATING.add(piece_id)
    try:
        return await _regenerate_image(campaign, piece, piece_id, workspace_id=workspace_id, user_id=user_id)
    finally:
        _REGENERATING.discard(piece_id)


#: Posts whose picture is being made again right now. A second click would make a second paid picture and leave a wrong chain of
#: replaced pictures.
_REGENERATING: set[str] = set()


async def _regenerate_image(
    campaign: dict[str, Any], piece: dict, piece_id: str, *, workspace_id: str, user_id: str,
) -> dict[str, Any]:
    from app.api.v1.image_assets import create_image_asset
    from app.db.mongo import image_assets, media_assets

    ctx = await _context(workspace_id, user_id)
    current = {"workspace_id": workspace_id, "source_piece_id": piece_id, "replaced_by": {"$exists": False}}
    old = await image_assets.find(current).to_list(length=None)
    old_ids = [d["id"] for d in old]
    old_media = [s["media_id"] for d in old for s in d.get("slides", []) if s.get("media_id")]
    old_real = bool(old_media) and await media_assets.count_documents({"id": {"$in": old_media}, "qa_flagged": {"$ne": True}}) > 0

    try:
        new = await create_image_asset(await _image_request(campaign, piece), ctx)
    except Exception as exc:  # noqa: BLE001 - never fail the post
        logger.warning("Campaign %s: regenerate image for piece %s failed: %s", campaign["id"], piece_id, exc)
        if not old_ids:
            await _record_status(workspace_id, piece_id, {"image": "failed"})
        return {"state": "failed", "note": "The picture could not be made. Try again in a moment."}

    new_media = [s.media_id for s in new.slides if s.media_id]
    flagged_docs = await media_assets.find({"id": {"$in": new_media}, "qa_flagged": True}).to_list(length=None)
    new_real = not flagged_docs
    note = (flagged_docs[0].get("qa_flag_reason") if flagged_docs else None)

    if old_real and not new_real:
        # keep the real picture; hide the card that was just made
        await image_assets.update_one({"id": new.id}, {"$set": {"replaced_by": "kept_previous"}})
        return {"state": "kept", "note": note}

    if old_ids:
        await image_assets.update_many({"id": {"$in": old_ids}}, {"$set": {"replaced_by": new.id}})
    # The new picture replaces the old one on the post itself, not only in the library.
    await attach_generated_image(piece_id, workspace_id, user_id, new.id, replace_ids=old_ids)
    await _record_status(workspace_id, piece_id, {"image": "ready"})
    return {"state": "ready" if new_real else "card", "note": note}


async def retry_media_for_piece(
    campaign: dict[str, Any], piece_id: str, *, workspace_id: str, user_id: str,
) -> dict[str, str]:
    """Try again only the kinds that failed for this post. Returns the new state of each kind tried."""
    piece = await content_pieces.find_one({"workspace_id": workspace_id, "piece_id": piece_id, "deleted": {"$ne": True}})
    if not piece or piece.get("campaign_id") != campaign["id"] or not (piece.get("content") or "").strip():
        return {}
    failed = [k for k, v in (piece.get("media_status") or {}).items() if v == "failed" and k in GENERATED_KINDS]
    kinds = [k for k in wanted_kinds(campaign.get("media_plan")) if k in failed]
    # Each kind is claimed with one conditional write (failed to making), so two clicks or two tabs cannot both make it.
    claimed: list[str] = []
    for kind in kinds:
        taken = await content_pieces.update_one(
            {"workspace_id": workspace_id, "piece_id": piece_id, f"media_status.{kind}": "failed"},
            {"$set": {f"media_status.{kind}": "making"}},
        )
        if taken.modified_count:
            claimed.append(kind)
    if not claimed:
        return {}
    ctx = await _context(workspace_id, user_id)
    try:
        states = await _make_for_piece(campaign, piece, claimed, ctx)
    except BaseException:
        await _record_status(workspace_id, piece_id, {k: "failed" for k in claimed})
        raise
    await _record_status(workspace_id, piece_id, states)
    return states
