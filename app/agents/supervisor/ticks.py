"""Cron bodies for the workspace supervisor, plus the workspace-scoped helpers
the worker and the smoke test call directly.

Two independent Redis Stream consumer groups on ``recast:events``:
  * ``supervisor_rules``  — drained by ``supervisor_rules_tick`` (1 min)
  * ``supervisor_reason`` — drained by ``supervisor_reason_tick`` (5 min)
so the deterministic and the LLM passes never steal each other's messages.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from pymongo.errors import DuplicateKeyError
from redis.exceptions import ResponseError

from app.agents.personal import thresholds as P_T
from app.agents.personal.signals import emit_signal, remy_message, supervisor_note
from app.agents.supervisor import notify as notify_mod
from app.agents.supervisor import thresholds as T
from app.agents.supervisor.graph import run_supervisor
from app.agents.supervisor.personas import flag_title
from app.agents.supervisor.platform_snapshot import gather_platform_snapshot
from app.agents.supervisor.rules import evaluate_rules
from app.shared.language import first_present, user_language, workspace_language
from app.db.mongo import (
    agent_worker_state,
    member_personas,
    personal_signals,
    workspace_events,
    workspace_flags,
    workspace_members,
    workspaces,
)
from app.db.redis import get_redis
from app.shared.events import EVENTS_STREAM

logger = logging.getLogger(__name__)

RULES_GROUP = "supervisor_rules"
REASON_GROUP = "supervisor_reason"
CONSUMER = "supervisor-1"
BATCH = 200
#: An event that was read but never handled (its check kept failing) is given up on after this long, so one broken workspace cannot be
#: retried for ever.
PENDING_MAX_AGE_MS = 60 * 60 * 1000


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _ensure_group(r, group: str) -> None:
    # Supervisor groups start at "$" (only events from group-creation onward).
    # Missing a few seconds of backlog at first deploy is fine — the rules and
    # digest both re-query the last 24h of workspace_events directly. This avoids
    # replaying the entire 90-day stream on the very first tick.
    try:
        await r.xgroup_create(EVENTS_STREAM, group, id="$", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _ack(r, group: str, entry_ids: list[str]) -> None:
    """Mark events as handled. Done only after a workspace's check ran, so a check that failed is tried again on the next tick."""
    if entry_ids:
        await r.xack(EVENTS_STREAM, group, *entry_ids)


async def _drain_group(r, group: str) -> dict[str, list[str]]:
    """Read the events *group* has not handled yet and return {workspace_id: [event ids]} (assistant.signal events excluded, and
    acknowledged at once because nothing is done for them).

    The events are NOT acknowledged here: the caller acknowledges a workspace's events (`_ack`) after it has handled that workspace.
    Events read before that were never acknowledged (the check failed, or the server stopped part way) come first, so nothing is lost;
    one older than PENDING_MAX_AGE_MS is given up on."""
    await _ensure_group(r, group)
    touched: dict[str, list[str]] = {}
    give_up: list[str] = []
    skip: list[str] = []
    now_ms = int(_now().timestamp() * 1000)

    def take(entries) -> Optional[str]:
        last = None
        for entry_id, fields in entries:
            last = entry_id
            ws, etype = fields.get("workspace_id"), fields.get("event_type")
            if not (ws and etype) or etype == "assistant.signal":
                skip.append(entry_id)
            elif now_ms - int(str(entry_id).split("-")[0]) > PENDING_MAX_AGE_MS:
                give_up.append(entry_id)
            else:
                touched.setdefault(ws, []).append(entry_id)
        return last

    # 1. events delivered before and never acknowledged
    cursor = "0"
    while True:
        resp = await r.xreadgroup(group, CONSUMER, {EVENTS_STREAM: cursor}, count=BATCH)
        entries = [e for _stream, es in (resp or []) for e in es]
        if not entries:
            break
        cursor = take(entries) or cursor
        if len(entries) < BATCH:
            break
    # 2. new events
    while True:
        resp = await r.xreadgroup(group, CONSUMER, {EVENTS_STREAM: ">"}, count=BATCH, block=50)
        entries = [e for _stream, es in (resp or []) for e in es]
        if not entries:
            break
        take(entries)
        if len(entries) < BATCH:
            break
    await _ack(r, group, skip + give_up)
    return touched


# ─────────────────────────────────────────────────────────────────────────────
# RULE PASS (1 min)
# ─────────────────────────────────────────────────────────────────────────────

async def run_rules_for_workspace(workspace_id: str) -> list[str]:
    """Evaluate every rule for a workspace, persist genuinely-new flags, deliver
    an admin notification for each. Returns the new flag ids.

    Found via audit: this was the one real production call site of
    evaluate_rules()/odette_flag_summary() that never passed a language at
    all, silently defaulting every rule-based flag to English regardless of
    the workspace. Fixed by resolving it the same way _gather_inputs() does.
    """
    ws_doc = await workspaces.find_one({"id": workspace_id}) or {}
    language = await _resolve_workspace_language(ws_doc)
    candidates = await evaluate_rules(workspace_id, language=language)
    if not candidates:
        return []

    open_types = set()
    async for f in workspace_flags.find(
        {"workspace_id": workspace_id, "status": "open"}, {"flag_type": 1}
    ):
        open_types.add(f["flag_type"])

    now = _now()
    new_ids: list[str] = []
    for c in candidates:
        if c["flag_type"] in open_types:
            continue
        fid = str(uuid4())
        await workspace_flags.insert_one({
            "_id": fid,
            "workspace_id": workspace_id,
            "flag_type": c["flag_type"],
            "detection": "rule",
            "severity": c["severity"],
            "summary_persona": c["summary_persona"],
            "detail": c["detail"],
            "metric": c["metric"],
            "langsmith_run_url": None,
            "status": "open",
            "notified": {"in_app": False, "email": False, "at": None},
            "created_at": now,
            "resolved_at": None,
        })
        nid = await notify_mod.deliver(
            workspace_id, kind="flag", source_id=fid,
            title=flag_title(c["flag_type"]),
            body_persona=c["summary_persona"], severity=c["severity"],
        )
        await workspace_flags.update_one(
            {"_id": fid},
            {"$set": {"notified": {"in_app": True, "email": c["severity"] == "critical", "at": now}}},
        )
        from app.agents.supervisor.nodes import _project_flag
        await _project_flag(fid)
        open_types.add(c["flag_type"])
        new_ids.append(fid)
        logger.info("supervisor rule flag: ws=%s type=%s severity=%s notif=%s",
                    workspace_id, c["flag_type"], c["severity"], nid)
    return new_ids


async def supervisor_rules_tick(ctx: dict) -> dict:
    """arq cron — every minute. Deterministic hard-limit flags."""
    r = await get_redis()
    touched = await _drain_group(r, RULES_GROUP)
    # Always sweep workspaces that saw activity; also re-check any with open flags
    # isn't needed here — resolution happens via the API.
    total_new = 0
    for ws, entry_ids in touched.items():
        try:
            total_new += len(await run_rules_for_workspace(ws))
            await _ack(r, RULES_GROUP, entry_ids)
        except Exception as exc:  # noqa: BLE001
            # left unacknowledged: the next tick reads these events again and tries this workspace again
            logger.error("rules tick failed for ws=%s: %s", ws, exc)
    if touched:
        logger.info("supervisor_rules_tick: %d workspaces, %d new flags", len(touched), total_new)
    return {"workspaces": len(touched), "new_flags": total_new}


# ─────────────────────────────────────────────────────────────────────────────
# REASONING PASS (5 min, debounced)
# ─────────────────────────────────────────────────────────────────────────────

async def _ws_state(workspace_id: str) -> dict:
    return await agent_worker_state.find_one({"_id": f"supervisor:{workspace_id}"}) or {
        "_id": f"supervisor:{workspace_id}",
        "events_since_last_llm": 0, "last_llm_pass_at": None, "lock_until": None,
    }


def _batch_trigger(st: dict, *, new_rule_flag: bool, high_sev_signal: bool, now: datetime) -> str:
    since = int(st.get("events_since_last_llm", 0))
    last = st.get("last_llm_pass_at")
    elapsed = (now - last).total_seconds() if last else 1e12
    if since >= T.BATCH_EVENT_COUNT:
        return "event_count"
    if new_rule_flag:
        return "rule_flag"
    if high_sev_signal:
        return "high_severity_signal"
    if elapsed >= T.BATCH_HEARTBEAT_S and since >= T.BATCH_HEARTBEAT_MIN_EVENTS:
        return "heartbeat"
    return ""


async def _resolve_workspace_language(workspace: dict) -> str:
    """The workspace's language for Odette's briefing.

    Precedence: (1) workspace.language, now that Stage 4 added it to the
    Workspace model — read directly off the already-fetched doc, no extra
    query; (2) the workspace owner's own users.language, as the best
    available proxy until an admin explicitly sets one (they set the
    workspace up; their preference is a reasonable stand-in for "this
    workspace's language"); (3) "en". Delegates the actual precedence
    resolution to app.shared.language, same primitives every other
    language-aware call site uses.
    """
    return first_present(
        workspace.get("language"),
        await user_language(workspace.get("owner_id")),
    )


async def _gather_inputs(workspace_id: str) -> dict:
    since = _now() - timedelta(hours=T.RULE_LOOKBACK_HOURS)
    events = await workspace_events.find({
        "workspace_id": workspace_id,
        "occurred_at": {"$gte": since.isoformat()},
        "event_type": {"$ne": "assistant.signal"},
    }).sort("occurred_at", -1).to_list(length=2000)
    signals = await personal_signals.find({
        "workspace_id": workspace_id, "created_at": {"$gte": since},
    }).sort("created_at", -1).to_list(length=1000)
    workspace = await workspaces.find_one({"id": workspace_id}) or {}
    active_members = await workspace_members.count_documents(
        {"workspace_id": workspace_id, "status": "active"}
    )
    open_flags = await workspace_flags.find(
        {"workspace_id": workspace_id, "status": "open"}
    ).to_list(length=100)
    language = await _resolve_workspace_language(workspace)
    platform_snapshot = await gather_platform_snapshot(workspace_id)
    return {"events": events, "signals": signals, "workspace": workspace,
            "active_members": active_members, "open_flags": open_flags,
            "language": language, "platform_snapshot": platform_snapshot}


async def _run_reasoning_pass(workspace_id: str, trigger: str) -> dict:
    now = _now()
    # The lock is taken with one conditional write, so the timed pass and "run now" can never both start for a workspace. The row
    # matches only when it holds no lock or the lock has run out; if it exists and is locked, the write cannot match, trying to add a
    # second row with the same id fails, and that means "locked".
    try:
        taken = await agent_worker_state.find_one_and_update(
            {"_id": f"supervisor:{workspace_id}", "$or": [{"lock_until": None}, {"lock_until": {"$lte": now}}, {"lock_until": {"$exists": False}}]},
            {"$set": {"lock_until": now + timedelta(seconds=T.LLM_PASS_LOCK_SECONDS), "updated_at": now}},
            upsert=True,
        )
    except DuplicateKeyError:
        taken = False
    if taken is False:
        logger.info("supervisor reasoning skipped (locked) ws=%s", workspace_id)
        return {"skipped": "locked"}
    inputs = await _gather_inputs(workspace_id)
    result = await run_supervisor(workspace_id, trigger=trigger, **inputs)
    # persist_node clears the lock + counters on success; clear defensively otherwise.
    await agent_worker_state.update_one(
        {"_id": f"supervisor:{workspace_id}"},
        {"$set": {"lock_until": None, "updated_at": _now()}},
    )
    return result.get("persisted", {"insight_ids": [], "flag_ids": [], "notification_ids": []})


async def supervisor_reason_tick(ctx: dict) -> dict:
    """arq cron — every 5 min. Debounced LLM reasoning pass per workspace."""
    r = await get_redis()
    touched = await _drain_group(r, REASON_GROUP)
    now = _now()
    ran = 0
    for ws, entry_ids in touched.items():
        n = len(entry_ids)
        try:
            st = await _ws_state(ws)
            since = int(st.get("events_since_last_llm", 0)) + n
            await agent_worker_state.update_one(
                {"_id": f"supervisor:{ws}"},
                {"$set": {"events_since_last_llm": since, "updated_at": now}},
                upsert=True,
            )
            st["events_since_last_llm"] = since

            new_rule_flag = await workspace_flags.find_one({
                "workspace_id": ws, "detection": "rule", "status": "open",
                "created_at": {"$gte": now - timedelta(seconds=T.REASON_TICK_SECONDS + 90)},
            }) is not None
            high_sev_signal = await personal_signals.find_one({
                "workspace_id": ws, "severity": "high",
                "created_at": {"$gte": now - timedelta(seconds=T.REASON_TICK_SECONDS + 90)},
            }) is not None

            trigger = _batch_trigger(st, new_rule_flag=new_rule_flag,
                                     high_sev_signal=high_sev_signal, now=now)
            if trigger:
                await _run_reasoning_pass(ws, trigger)
                ran += 1
            await _ack(r, REASON_GROUP, entry_ids)
        except Exception as exc:  # noqa: BLE001
            # left unacknowledged: the next tick reads these events again and tries this workspace again
            logger.error("reason tick failed for ws=%s: %s", ws, exc, exc_info=True)
    if touched:
        logger.info("supervisor_reason_tick: %d workspaces touched, %d reasoning passes", len(touched), ran)
    return {"workspaces": len(touched), "passes": ran}


async def run_supervisor_now(ctx: dict, workspace_id: str) -> dict:
    """Enqueueable on-demand pass (POST /supervisor/run). Ignores the batch
    trigger but still respects the coalescing lock."""
    return await _run_reasoning_pass(workspace_id, trigger="manual")


# ─────────────────────────────────────────────────────────────────────────────
# volume_drop periodic sweep — moved off the per-piece personal graph
# ─────────────────────────────────────────────────────────────────────────────

async def personal_volume_sweep(ctx: dict) -> dict:
    """Every few hours: find members who used to post regularly and have gone
    quiet, and raise one ``volume_drop`` signal each (deduped)."""
    now = _now()
    raised = 0
    # only members who used to post enough for a drop to mean anything are read at all
    cursor = member_personas.find(
        {"volume_stats.mean": {"$gte": P_T.VOLUME_DROP_BASELINE_PER_DAY}}, {"workspace_id": 1, "user_id": 1, "volume_stats": 1}
    )
    member_cache: dict[tuple[str, str], bool] = {}
    async for p in cursor:
        if not await _still_a_member(p["workspace_id"], p["user_id"], member_cache):
            continue
        vs = p.get("volume_stats", {}) or {}
        daily = vs.get("daily_counts", {}) or {}
        mean_v = float(vs.get("mean", 0.0) or 0.0)
        if mean_v < P_T.VOLUME_DROP_BASELINE_PER_DAY:
            continue
        quiet = all(
            daily.get((now - timedelta(days=i)).strftime("%Y-%m-%d"), 0) == 0
            for i in range(0, P_T.VOLUME_DROP_ZERO_DAYS)
        )
        if not quiet:
            continue
        dup = await personal_signals.find_one({
            "workspace_id": p["workspace_id"], "user_id": p["user_id"],
            "signal_type": "volume_drop",
            "created_at": {"$gte": now - timedelta(hours=T.VOLUME_DROP_DEDUP_HOURS)},
        })
        if dup:
            continue
        ctx_ = {"zero_days": P_T.VOLUME_DROP_ZERO_DAYS, "baseline_per_day": round(mean_v, 1)}
        # This sweep calls remy_message() directly (it never runs the personal
        # graph / PersonaState), so it needs its own member-language lookup —
        # same precedence as app.agents.personal.state._resolve_member_language:
        # the member's own preference beats the workspace default (Remy speaks
        # to one person about their own work), falling through to "en".
        member_language = first_present(
            await user_language(p["user_id"]),
            await workspace_language(p["workspace_id"]),
        )
        await emit_signal(
            workspace_id=p["workspace_id"], user_id=p["user_id"], pipeline_type=None,
            signal_type="volume_drop", severity="medium",
            metric={"name": "zero_days", "value": float(P_T.VOLUME_DROP_ZERO_DAYS),
                    "baseline": round(mean_v, 3), "threshold": float(P_T.VOLUME_DROP_ZERO_DAYS)},
            window={"kind": "rolling", "n": P_T.VOLUME_WINDOW_DAYS},
            evidence_refs=[],
            member_message=await remy_message("volume_drop", ctx=ctx_, language=member_language),
            supervisor_note=supervisor_note("volume_drop", ctx=ctx_),
            dedupe_key=f"volume_drop:{p['workspace_id']}:{p['user_id']}:{now.strftime('%Y-%m-%d')}",
        )
        raised += 1
    raised += await _platform_volume_sweep(now)
    if raised:
        logger.info("personal_volume_sweep: raised %d volume_drop signals", raised)
    return {"volume_drop_signals": raised}


async def _still_a_member(workspace_id: str, user_id: str, cache: dict) -> bool:
    """Whether the person is still an active member of the workspace. Someone who left, or was removed, is not told about their volume."""
    key = (workspace_id, user_id)
    if key not in cache:
        cache[key] = bool(await workspace_members.find_one({"workspace_id": workspace_id, "user_id": user_id, "status": "active"}, {"_id": 1}))
    return cache[key]


async def _platform_volume_sweep(now: datetime) -> int:
    """Per-platform half of personal_volume_sweep — same "used to post regularly,
    now quiet" logic as the overall sweep above, but scoped to one platform at a
    time (member_personas.volume_stats.daily_counts_by_platform, see
    app.agents.personal.persona_store.apply_piece), so a member who's still
    active on LinkedIn but has gone quiet on Instagram specifically still gets
    a signal — the overall sweep's combined daily_counts would mask that."""
    raised = 0
    cursor = member_personas.find(
        {"volume_stats.daily_counts_by_platform": {"$exists": True}}, {"workspace_id": 1, "user_id": 1, "volume_stats.daily_counts_by_platform": 1}
    )
    member_cache: dict[tuple[str, str], bool] = {}
    async for p in cursor:
        if not await _still_a_member(p["workspace_id"], p["user_id"], member_cache):
            continue
        by_platform = (p.get("volume_stats", {}) or {}).get("daily_counts_by_platform", {}) or {}
        for platform, vs in by_platform.items():
            mean_v = float(vs.get("mean", 0.0) or 0.0)
            if mean_v < P_T.VOLUME_DROP_BASELINE_PER_DAY:
                continue
            daily = vs.get("daily_counts", {}) or {}
            quiet = all(
                daily.get((now - timedelta(days=i)).strftime("%Y-%m-%d"), 0) == 0
                for i in range(0, P_T.VOLUME_DROP_ZERO_DAYS)
            )
            if not quiet:
                continue
            dup = await personal_signals.find_one({
                "workspace_id": p["workspace_id"], "user_id": p["user_id"],
                "signal_type": "platform_volume_drop",
                "metric.platform": platform,
                "created_at": {"$gte": now - timedelta(hours=T.VOLUME_DROP_DEDUP_HOURS)},
            })
            if dup:
                continue
            ctx_ = {
                "platform": platform,
                "zero_days": P_T.VOLUME_DROP_ZERO_DAYS,
                "baseline_per_day": round(mean_v, 1),
            }
            member_language = first_present(
                await user_language(p["user_id"]),
                await workspace_language(p["workspace_id"]),
            )
            await emit_signal(
                workspace_id=p["workspace_id"], user_id=p["user_id"], pipeline_type=None,
                signal_type="platform_volume_drop", severity="low",
                metric={"name": "zero_days", "value": float(P_T.VOLUME_DROP_ZERO_DAYS),
                        "baseline": round(mean_v, 3), "threshold": float(P_T.VOLUME_DROP_ZERO_DAYS),
                        "platform": platform},
                window={"kind": "rolling", "n": P_T.VOLUME_WINDOW_DAYS},
                evidence_refs=[],
                member_message=await remy_message("platform_volume_drop", ctx=ctx_, language=member_language),
                supervisor_note=supervisor_note("platform_volume_drop", ctx=ctx_),
                dedupe_key=f"platform_volume_drop:{p['workspace_id']}:{p['user_id']}:{platform}:{now.strftime('%Y-%m-%d')}",
            )
            raised += 1
    return raised
