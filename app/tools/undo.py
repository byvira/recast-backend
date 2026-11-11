"""Undo for every change made through a tool.

Every tool that changes anything declares how a person takes the change back (UNDO below; a test fails if a tool has no entry). Four
ways, plus two honest ones:

* VERSION  a post, picture, video or recording keeps its versions. The version before the change is remembered; undo restores it as a new
           version, so nothing is lost and the undo can itself be undone. Any earlier version can also be restored with that thing's own
           restore tool.
* SNAPSHOT a saved copy of the settings document is taken before the change and put back by undo. Refused if the document changed again
           since (undo the newer change first), so newer work is never overwritten.
* OPPOSITE the tool that does the reverse (archive and bring back, schedule and cancel the schedule).
* CREATES  something new was made; undo removes it with that thing's own delete tool (which a key needs the delete capability for).
* ADDS     only adds results (writing, scoring, planning); nothing existing changed, so there is nothing to put back. The reason says what to
           remove if it is not wanted.
* FINAL    cannot be taken back by Ekavo (something went out, credits were spent, an answer was recorded). The reason says what to do instead.

Every change is written to `tool_changes` (kept 90 days) with what is needed to undo it. An undo is a change too."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

from app.db.mongo import get_db


class Kind(str, Enum):
    VERSION = "version"
    SNAPSHOT = "snapshot"
    OPPOSITE = "opposite"
    CREATES = "creates"
    ADDS = "adds"
    FINAL = "final"


@dataclass(frozen=True)
class Rule:
    kind: Kind
    #: VERSION: which family, and the input field that names it
    entity: str = ""
    id_arg: str = ""
    #: SNAPSHOT: the collection, and how its one document is found: (document field, where the value comes from). A value is the name of an
    #: input field, "@user" for the person acting, or "=text" for a fixed value. The workspace is always part of the match.
    collection: str = ""
    match: tuple[tuple[str, str], ...] = ()
    #: OPPOSITE: the tool that reverses it, and how its input is worked out from this call's input and answer
    inverse: str = ""
    inverse_args: Optional[Callable[[dict[str, Any], Any], dict[str, Any]]] = None
    #: CREATES: the tool that removes what was made, and the answer fields that hold its id
    remove_tool: str = ""
    id_keys: tuple[str, ...] = ("id",)
    remove_arg: str = ""
    extra_args: tuple[tuple[str, Any], ...] = ()
    #: ADDS and FINAL: the plain words
    reason: str = ""


# ── the four families that keep versions ─────────────────────────────────

_FAMILIES: dict[str, tuple[str, str, str]] = {
    # family: (read route with {id}, version field, restore route with {id} and {n})
    "piece": ("/api/v1/content/pieces/{id}", "version_count", "/api/v1/content/pieces/{id}/restore/{n}"),
    "image": ("/api/v1/image-assets/{id}", "version_count", "/api/v1/image-assets/{id}/restore/{n}"),
    "audio": ("/api/v1/audio-assets/{id}", "version_count", "/api/v1/audio-assets/{id}/restore/{n}"),
    "video": ("/api/v1/video-projects/{id}", "version", "/api/v1/video-projects/{id}/versions/{n}/restore"),
}


def _v(entity: str, id_arg: str) -> Rule:
    return Rule(Kind.VERSION, entity=entity, id_arg=id_arg)


def _snap(collection: str, *match: tuple[str, str]) -> Rule:
    return Rule(Kind.SNAPSHOT, collection=collection, match=match)


def _creates(remove_tool: str, *id_keys: str, remove_arg: str, **extra: Any) -> Rule:
    return Rule(Kind.CREATES, remove_tool=remove_tool, id_keys=id_keys or ("id",), remove_arg=remove_arg, extra_args=tuple(extra.items()))


def _adds(reason: str) -> Rule:
    return Rule(Kind.ADDS, reason=reason)


def _final(reason: str) -> Rule:
    return Rule(Kind.FINAL, reason=reason)


def _opposite(inverse: str, args: Callable[[dict[str, Any], Any], dict[str, Any]]) -> Rule:
    return Rule(Kind.OPPOSITE, inverse=inverse, inverse_args=args)


_NEW = "Writes or works something out and keeps it as new content; nothing that existed changed. Remove it with its delete tool if it is not wanted."
_DECISION = "A decision is recorded. Change it by deciding again in Ekavo."
_SENT = "It has gone out or been recorded outside Ekavo, so Ekavo cannot take it back."
_STATE = "It only updates a status or a read marker. Nothing was written to the content."
_MADE = "It made files from what exists; the originals are untouched. Remove the new file from the library if it is not wanted."


def _same(**flip: bool):
    """The same tool with a yes/no field turned the other way."""

    def build(args: dict[str, Any], data: Any) -> dict[str, Any]:
        out = {k: v for k, v in args.items() if k not in flip}
        for key in flip:
            out[key] = not bool(args.get(key))
        return out

    return build


def _ids_only(*names: str):
    def build(args: dict[str, Any], data: Any) -> dict[str, Any]:
        return {k: args[k] for k in names if k in args}

    return build


UNDO: dict[str, Rule] = {
    # posts: a post keeps its versions
    "library.edit_post": _v("piece", "piece_id"), "library.save_document": _v("piece", "piece_id"), "library.restore_version": _v("piece", "piece_id"),
    "text.refine": _v("piece", "piece_id"), "text.refine_chat": _v("piece", "piece_id"), "text.regenerate": _v("piece", "piece_id"),
    "library.regenerate_picture": _v("piece", "piece_id"),
    # pictures
    **{n: _v("image", "image_asset_id") for n in (
        "image.set_headline", "image.set_colors", "image.apply_look", "image.set_logo_spot", "image.add_big_number", "image.add_chart", "image.reshape",
        "image.reset", "image.restore_version", "image.apply_looks_to_set", "image.add_slide", "image.delete_slide", "image.reorder_slides")},
    # videos
    **{n: _v("video", "project_id") for n in (
        "video.trim_clip", "video.reorder_clips", "video.add_text", "video.set_captions_style", "video.set_transition", "video.add_music", "video.set_look",
        "video.set_canvas", "video.add_bleeps", "video.set_emphasis", "video.restore_version", "video.reset")},
    # recordings
    **{n: _v("audio", "audio_asset_id") for n in (
        "audio.cleanup", "audio.assemble", "audio.regenerate", "audio.localize", "audio.restore_version")},
    # settings that keep no versions: a saved copy
    **{n: _snap("brand_profiles", ("id", "brand_id")) for n in (
        "brand.save_section", "brand.set_voice", "brand.set_calibration", "brand.set_visual_identity", "brand.set_logo", "brand.set_watermark",
        "brand.set_language", "brand.set_safety_profile", "brand.set_type", "brand.set_active", "brand.set_default", "brand.save_step", "brand.complete",
        "brand.add_training_sample", "brand.remove_training_sample", "brand.regenerate_mascot", "brand.make_watermark_mark")},
    "campaigns.update": _snap("campaigns", ("id", "campaign_id")), "presets.update": _snap("presets", ("id", "preset_id")),
    **{n: _snap("image_assets", ("id", "image_asset_id")) for n in ("image.rename", "image.set_details", "image.set_slide_role")},
    **{n: _snap("audio_assets", ("id", "audio_asset_id")) for n in ("audio.rename", "audio.update_video_clip", "audio.correct_word")},
    **{n: _snap("content_pieces", ("piece_id", "piece_id")) for n in (
        "library.attach", "library.detach", "library.reorder_attachments", "library.update_attachment", "library.set_post_media")},
    "library.rename_run": _snap("content_sessions", ("session_id", "session_id")),
    "assistant.set_lexicon": _snap("member_lexicon", ("user_id", "@user")),
    "assistant.set_voice_settings": _snap("member_voice_settings", ("user_id", "@user")),
    "audio.save_kit": _snap("audio_kits", ("brand_id", "brand_id")), "audio.add_bed": _snap("audio_kits", ("brand_id", "brand_id")),
    "audio.remove_bed": _snap("audio_kits", ("brand_id", "brand_id")),
    "audio.update_feed": _snap("podcast_feed_settings", ("brand_id", "brand_id")), "audio.enable_feed": _snap("podcast_feed_settings", ("brand_id", "brand_id")),
    "platforms.record_listing": _snap("platform_listings", ("platform_key", "key")),
    "analytics.record_results": _snap("post_metrics", ("post_id", "piece_id"), ("source", "=self_reported")),
    "flows.automate_recipe": _snap("video_recipes", ("id", "recipe_id")),
    # the opposite tool
    "library.archive_post": _opposite("library.archive_post", _same(archived=True)),
    "content.bulk_archive": _opposite("content.bulk_archive", _same(archived=True)),
    "library.schedule_post": _opposite("library.cancel_schedule", _ids_only("piece_id")),
    "runs.control": _final("A run is paused, resumed or stopped. Resume a paused run with the same tool; a stopped run cannot be revived, start it again."),
    # something new was made: remove it
    "library.create_draft": _creates("library.delete_post", "piece_id", "id", remove_arg="piece_id", confirm=True),
    "compose.run": _adds("Each step is recorded as its own change, with its own undo; list them with changes.list."),
    "compose.resume": _adds("Each step that runs again is recorded as its own change, with its own undo."),
    "runs.retry": _adds("Starts the same work again as a new run. Stop it with runs.control if it is not wanted."),
    "changes.undo": _final("An undo is recorded as a change of its own. Undo that one to go forward again."),
    "campaigns.create": _creates("campaigns.delete", "id", "campaign_id", remove_arg="campaign_id", confirm=True),
    "presets.create": _creates("presets.delete", "id", "preset_id", remove_arg="preset_id", confirm=True),
    "video.create_project": _creates("video.delete_project", "id", "project_id", remove_arg="project_id", confirm=True),
    "notes.add": _creates("notes.remove", "id", "comment_id", remove_arg="comment_id"),
    "flows.save_recipe": _creates("flows.delete_recipe", "id", "recipe_id", remove_arg="recipe_id", confirm=True),
    "image.save_template": _creates("image.delete_template", "id", "template_id", remove_arg="template_id", confirm=True),
    "brand.create": _final("A new brand is removed in the app (brand deletion stays there)."),
    "brand.duplicate": _final("A copy is removed in the app (brand deletion stays there)."),
    # additive writing, scoring and planning
    **{n: _adds(_NEW) for n in (
        "text.generate", "text.repurpose", "text.batch", "text.angles", "text.angle_ideas", "text.angle_make", "text.preview_link", "text.reader_review",
        "text.score_hook", "text.score_readability", "text.suggest_repurpose", "audio.generate", "audio.dialogue", "audio.import", "audio.director",
        "audio.transcribe", "audio.chapters", "audio.derivatives", "audio.suggest_clips", "audio.find_soundbites", "audio.refine_soundbites",
        "audio.fit_script", "audio.length_preview", "audio.list_feed_episodes", "audio.sponsor_voice", "audio.video", "audio.promote", "audio.send_to_draft",
        "image.generate", "image.send_to_draft", "campaign.next_batch", "campaign.regenerate_media", "campaign.retry_media", "campaigns.suggest_topics",
        "covers.compose", "covers.frames", "covers.make", "flows.run", "flows.batch", "flows.run_recipe", "media.analyze", "media.transform", "reframe.make",
        "safety.check", "search.ask", "stock.rail_options", "stock.use", "transcripts.create", "transcripts.retry", "video.broll", "video.chain",
        "video.critique", "video.director", "video.edit_plan", "video.hooks", "video.polish_captions", "video.publish_text", "video.render", "video.script",
        "video.script_from_pictures", "video.voice_check", "videoanalysis.run", "videolib.preview", "videolib.import_link", "videoprompt.draft",
        "videoprompt.make", "videovoice.localize_captions", "videovoice.make_dub", "videovoice.narrate", "brand.preview_rewrite", "brand.suggest_voice_patterns",
        "brand.watermark_suggestions", "presets.suggest_structure", "publish.youtube_prepare", "analytics.ask", "analytics.refresh", "assistant.assist",
        "image.export", "audio.export", "sfx.add", "library.check_link", "library.write_first_comment", "video.learn_recording_voice")},
    # small records with no copy kept; each says what to do
    **{n: _final(_STATE) for n in (
        "activity.mark_inbox_read", "activity.mark_read", "assistant.acknowledge_signal", "supervisor.read_notification", "supervisor.set_flag_status",
        "supervisor.set_insight_status", "video.cancel_job", "videoanalysis.set_moment", "library.mark_posted", "library.post_feedback",
        "safety.request_review", "videoreview.reply", "notes.reply", "image.edit_comment", "audio.edit_comment", "library.refresh_attachments",
        "audio.update_share_link", "video.set_recording_voice", "videogov.set_privacy", "repurposes.save", "videoreview.resolve_comment", "notes.resolve")},
    "library.cancel_schedule": _final("To schedule it again, schedule the post: that needs a person's yes."),
    "audio.comment": _creates("audio.delete_comment", "id", "comment_id", remove_arg="comment_id", audio_asset_id=None),
    "image.comment": _creates("image.delete_comment", "id", "comment_id", remove_arg="comment_id", image_asset_id=None),
    "audio.delete_comment": _final("A removed note cannot be brought back."), "image.delete_comment": _final("A removed note cannot be brought back."),
    "notes.remove": _final("A removed note cannot be brought back."),
    # the share links: end the link, or make a new one
    "audio.share": _creates("audio.end_share_link", "token", remove_arg="token", audio_asset_id=None),
    "image.share": _creates("image.end_share_link", "token", remove_arg="token", image_asset_id=None),
    "videoreview.create_link": _creates("videoreview.end_link", "id", "link_id", remove_arg="link_id"),
    "audio.end_share_link": _final("Make a new link with audio.share."), "image.end_share_link": _final("Make a new link with image.share."),
    "videoreview.end_link": _final("Make a new link with videoreview.create_link."),
    # what leaves Ekavo, and what only a person decides
    **{n: _final(_SENT) for n in ("publish.now", "publish.batch", "publish.send_to_destination", "publish.youtube_new_playlist")},
    **{n: _final(_DECISION) for n in (
        "activity.decide", "activity.set_autonomy_threshold", "audio.approve", "audio.reject", "audio.sign_off", "audio.remove_sign_off",
        "audio.approve_soundbite", "audio.reject_soundbite", "image.approve", "image.reject", "library.approve_post", "library.reject_post",
        "library.approve_many", "library.approve_waiting", "library.approve_run", "video.approve", "videogov.add_consent", "videogov.withdraw_consent")},
    # removing things: only support restores a soft delete
    **{n: _final("It was removed. Only support can restore it.") for n in (
        "activity.hide", "audio.delete", "audio.delete_video_clip", "campaigns.delete", "content.bulk_delete", "flows.delete_recipe", "image.delete",
        "image.delete_template", "library.delete_post", "library.delete_run", "presets.delete", "repurposes.remove", "video.delete_project",
        "video.forget_recording_voice", "videolib.delete")},
}

def rule_for(tool: str) -> Optional[Rule]:
    return UNDO.get(tool)


def words(tool: str) -> str:
    """What undoing this tool looks like, in plain words, for the catalog and for the answer a call gives."""
    rule = UNDO.get(tool)
    if rule is None:
        return ""
    return {
        Kind.VERSION: "Undo restores the version from before the change. Any earlier version can be restored too, and an undo can be undone.",
        Kind.SNAPSHOT: "Undo puts back the saved copy taken before the change, unless it was changed again since.",
        Kind.OPPOSITE: f"Undo runs {rule.inverse}.",
        Kind.CREATES: f"Undo removes what was made, with {rule.remove_tool}.",
    }.get(rule.kind, rule.reason)


def can_undo(tool: str) -> bool:
    rule = UNDO.get(tool)
    return rule is not None and rule.kind in (Kind.VERSION, Kind.SNAPSHOT, Kind.OPPOSITE, Kind.CREATES)


# ── the records ──────────────────────────────────────────────────────────

CHANGE_DAYS = 90
MAX_COPY_BYTES = 400_000
_VOLATILE = {"_id", "updated_at"}
_KEEP = {"id", "workspace_id", "created_at", "user_id"}


def changes():
    return get_db()["tool_changes"]


async def create_indexes() -> None:
    await changes().create_index("at", expireAfterSeconds=CHANGE_DAYS * 24 * 3600)
    await changes().create_index([("workspace_id", 1), ("at", -1)])
    await changes().create_index([("workspace_id", 1), ("target.id", 1), ("at", -1)])
    await changes().create_index("id", unique=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k not in _VOLATILE}


def digest(doc: Optional[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(_clean(doc or {}), sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _filter(rule: Rule, workspace_id: str, user_id: Optional[str], arguments: dict[str, Any]) -> Optional[dict[str, Any]]:
    """How the one document is found. None when an input the match needs was not given."""
    flt: dict[str, Any] = {"workspace_id": workspace_id}
    for field, source in rule.match:
        value = user_id if source == "@user" else source[1:] if source.startswith("=") else arguments.get(source)
        if not value:
            return None
        flt[field] = value
    return flt


async def read_document(rule: Rule, flt: dict[str, Any]) -> Optional[dict[str, Any]]:
    return await get_db()[rule.collection].find_one(flt, {"_id": 0})


async def put_document(rule: Rule, flt: dict[str, Any], saved: Optional[dict[str, Any]]) -> None:
    """Put a saved copy back: every field in the copy is set, and fields the copy never had are removed. A copy of "nothing there" removes
    the document."""
    if saved is None:
        await get_db()[rule.collection].delete_one(flt)
        return
    current = await read_document(rule, flt) or {}
    restore = {k: v for k, v in _clean(saved).items() if k not in _KEEP}
    drop = {k: "" for k in current if k not in saved and k not in _KEEP and k not in _VOLATILE}
    update: dict[str, Any] = {"$set": {**restore, "updated_at": _now()}}
    if drop:
        update["$unset"] = drop
    await get_db()[rule.collection].update_one(flt, update, upsert=True)


def target_of(rule: Rule, arguments: dict[str, Any]) -> Optional[dict[str, Any]]:
    if rule.kind is Kind.VERSION:
        return {"type": rule.entity, "id": arguments.get(rule.id_arg)}
    if rule.kind is Kind.SNAPSHOT:
        return {"type": rule.collection, "id": next((arguments.get(src) for _, src in rule.match if not src.startswith(("@", "="))), None)}
    return None


async def current_version(rule: Rule, credential: Any, entity_id: str, request) -> Optional[int]:
    """The version a thing is on now, read through the app's own route as the person behind the credential."""
    read, field, _ = _FAMILIES[rule.entity]
    data = await request(credential, "GET", read.format(id=entity_id), None, None)
    value = data.get(field) if isinstance(data, dict) else None
    return int(value) if value is not None else None


async def capture(rule: Rule, admission: Any, arguments: dict[str, Any], request) -> Optional[dict[str, Any]]:
    """What undo needs, taken before the work runs. None when it cannot be taken (the call then fails by itself, or cannot be undone)."""
    credential = admission.credential
    if rule.kind is Kind.VERSION:
        entity_id = arguments.get(rule.id_arg)
        if not entity_id:
            return None
        try:
            version = await current_version(rule, credential, entity_id, request)
        except Exception:  # noqa: BLE001 - a read that fails here fails the call itself with the app's words
            return None
        return {"version": version} if version is not None else None
    if rule.kind is Kind.SNAPSHOT:
        flt = _filter(rule, credential.workspace_id, credential.user_id, arguments)
        if flt is None:
            return None
        doc = await read_document(rule, flt)
        if doc is not None and len(json.dumps(doc, default=str)) > MAX_COPY_BYTES:
            return None
        return {"doc": doc, "filter": flt}
    return None


async def record(rule: Rule, admission: Any, arguments: dict[str, Any], before: Optional[dict[str, Any]], data: Any, call_id: Optional[str]) -> dict[str, Any]:
    """Write the change row and say, in the form the caller sees, how to undo it."""
    credential, tool = admission.credential, admission.tool
    row: dict[str, Any] = {
        "id": uuid4().hex, "at": _now(), "workspace_id": credential.workspace_id, "user_id": credential.user_id, "credential_id": credential.id,
        "surface": credential.surface, "tool": tool.name, "call_id": call_id, "kind": rule.kind.value, "undone_by": None,
        "target": target_of(rule, arguments), "fields": sorted(arguments.keys()), "how": words(tool.name), "undoable": can_undo(tool.name),
    }
    if rule.kind is Kind.VERSION:
        row["before"] = before
        row["undoable"] = before is not None
    elif rule.kind is Kind.SNAPSHOT:
        row["before"] = before
        row["undoable"] = before is not None
        if before is not None:
            row["after_hash"] = digest(await read_document(rule, before["filter"]))
    elif rule.kind is Kind.OPPOSITE:
        row["inverse"] = {"tool": rule.inverse, "arguments": rule.inverse_args(arguments, data) if rule.inverse_args else {}}
    elif rule.kind is Kind.CREATES:
        made = _find_id(data, rule.id_keys)
        row["made_id"] = made
        inverse_args = {key: (arguments.get(key) if fixed is None else fixed) for key, fixed in rule.extra_args}
        inverse_args[rule.remove_arg] = made
        row["inverse"] = {"tool": rule.remove_tool, "arguments": inverse_args}
        row["undoable"] = bool(made)
    await changes().insert_one(dict(row))
    return {"id": row["id"], "undo": rule.kind.value, "undoable": row["undoable"], "how": row["how"]}


def _find_id(data: Any, keys: tuple[str, ...]) -> Optional[str]:
    """The id of what a call made, looked for in its answer (the answer itself or the item inside it)."""
    pools = [data] if isinstance(data, dict) else []
    if isinstance(data, dict):
        pools += [v for v in data.values() if isinstance(v, dict)]
        pools += [i for v in data.values() if isinstance(v, list) for i in v[:1] if isinstance(i, dict)]
    for pool in pools:
        for key in keys:
            if isinstance(pool.get(key), str) and pool[key]:
                return pool[key]
    return None
