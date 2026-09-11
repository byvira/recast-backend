"""Workspace-supervisor graph nodes.

    build_digest → reason (ReAct tool loop) → synthesize → persist → END

The ReAct loop is implemented directly on the Groq SDK's function-calling
(the codebase has no langchain chat-model dependency). It is still a real
multi-step investigate-then-conclude loop, capped at MAX_TOOL_CALLS.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from app.agents.supervisor import notify as notify_mod
from app.agents.supervisor import thresholds as T
from app.agents.supervisor.digest import build_digest, digest_is_quiet
from app.agents.supervisor.personas import build_odette_system
from app.agents.supervisor.state import SupervisorState
from app.agents.supervisor.tools import make_tools
from app.db.mongo import agent_worker_state, workspace_flags, workspace_insights
from app.shared.llm import GroqModel, get_groq_client
from app.utils.jsonparser import parse_llm_json

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 1. build_digest
# ─────────────────────────────────────────────────────────────────────────────

def build_digest_node(state: SupervisorState) -> dict:
    digest = build_digest(
        state["workspace_id"],
        events=state["events"],
        signals=state["signals"],
        workspace=state["workspace"],
        active_members=state["active_members"],
        open_flags=state["open_flags"],
    )
    return {"digest": digest}


# ─────────────────────────────────────────────────────────────────────────────
# 2. reason — ReAct tool loop on the raw Groq SDK
# ─────────────────────────────────────────────────────────────────────────────

async def reason_node(state: SupervisorState) -> dict:
    digest = state["digest"]
    if digest_is_quiet(digest):
        return {"scratchpad": [], "tool_calls_made": 0}

    specs, dispatch = make_tools(state["workspace_id"])
    client = get_groq_client()
    messages: list[dict] = [
        {"role": "system", "content": build_odette_system(state.get("language", "en"))},
        {"role": "user", "content":
            "DIGEST (this workspace, recent activity):\n"
            + json.dumps(digest, default=str, indent=2)
            + "\n\nInvestigate anything anomalous with the tools (a few targeted calls), "
              "then stop. Do not write the briefing yet."},
    ]
    calls = 0
    try:
        while calls < T.MAX_TOOL_CALLS:
            resp = await _groq_chat(client, messages, tools=specs, tool_choice="auto",
                                    max_tokens=T.REASON_MAX_TOKENS)
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None) or []
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                **({"tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls]} if tool_calls else {}),
            })
            if not tool_calls:
                break
            for tc in tool_calls:
                calls += 1
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await dispatch(tc.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str)[:T.TOOL_RESULT_CHAR_CAP],
                })
                if calls >= T.MAX_TOOL_CALLS:
                    break
    except Exception as exc:  # noqa: BLE001
        logger.error("supervisor reason loop failed (continuing to synthesis): %s", exc)
        return {"scratchpad": messages, "tool_calls_made": calls,
                "errors": state["errors"] + [f"reason: {exc}"]}

    logger.info("supervisor reason: ws=%s tool_calls=%d", state["workspace_id"], calls)
    return {"scratchpad": messages, "tool_calls_made": calls}


# ─────────────────────────────────────────────────────────────────────────────
# 3. synthesize — one structured Groq call → findings
# ─────────────────────────────────────────────────────────────────────────────

_SYNTH_INSTRUCTIONS = (
    "Now write the admin briefing for THIS workspace as Odette. Return JSON only:\n"
    '{\n'
    '  "insights": [{"kind": "recommendation"|"observation", "title": "<=70 chars", '
    '"body": "<=90 words, Odette voice, ends on a recommended action", '
    '"rationale": "<=40 words, why you concluded this", '
    '"priority": "low"|"medium"|"high", "pipeline_scope": ["text"] or "all"}],\n'
    '  "flags": [{"severity": "warning"|"critical", "summary": "<=60 words, Odette voice", '
    '"detail": {<structured evidence>}}],\n'
    '  "notify": true|false\n'
    '}\n'
    "Only raise a flag for a genuine anomaly the rule engine would miss (unusual pattern, "
    "coordinated shift, emerging risk). 0-3 insights, 0-2 flags. If nothing is worth an "
    "admin's attention, return empty arrays and notify=false."
)


async def synthesize_node(state: SupervisorState) -> dict:
    digest = state["digest"]
    if digest_is_quiet(digest):
        return {"findings": {"insights": [], "flags": [], "notify": False}}

    client = get_groq_client()
    messages = list(state["scratchpad"]) or [
        {"role": "system", "content": build_odette_system(state.get("language", "en"))},
        {"role": "user", "content": "DIGEST:\n" + json.dumps(digest, default=str)},
    ]
    messages.append({"role": "user", "content": _SYNTH_INSTRUCTIONS})

    findings = {"insights": [], "flags": [], "notify": False}
    try:
        resp = await _groq_chat(client, messages, max_tokens=T.SYNTH_MAX_TOKENS, temperature=0.4)
        parsed = parse_llm_json(resp.choices[0].message.content or "")
        if isinstance(parsed, dict):
            findings["insights"] = parsed.get("insights") or []
            findings["flags"] = parsed.get("flags") or []
            findings["notify"] = bool(parsed.get("notify", False))
    except Exception as exc:  # noqa: BLE001
        logger.error("supervisor synthesize failed: %s", exc)
        return {"findings": findings, "errors": state["errors"] + [f"synthesize: {exc}"]}

    return {"findings": findings}


# ─────────────────────────────────────────────────────────────────────────────
# 4. persist
# ─────────────────────────────────────────────────────────────────────────────

async def persist_node(state: SupervisorState) -> dict:
    ws = state["workspace_id"]
    now = datetime.now(timezone.utc)
    findings = state["findings"]
    run_url = state.get("langsmith_run_url", "")
    run_id = f"supervisor:{ws}:{now.isoformat()}"

    insight_ids: list[str] = []
    for ins in findings.get("insights", [])[:3]:
        iid = str(uuid4())
        await workspace_insights.insert_one({
            "_id": iid,
            "workspace_id": ws,
            "kind": ins.get("kind", "recommendation"),
            "title": (ins.get("title") or "Workspace note")[:200],
            "body_persona": ins.get("body", ""),
            "rationale": ins.get("rationale", ""),
            "evidence": {
                "event_ids": [e.get("event_id") for e in state["events"][:50] if e.get("event_id")],
                "signal_ids": [s.get("_id") for s in state["signals"][:50] if s.get("_id")],
                "metrics": {"window_events": state["digest"].get("window_events", 0)},
            },
            "priority": ins.get("priority", "medium"),
            "pipeline_scope": ins.get("pipeline_scope", "all"),
            "langsmith_run_url": run_url,
            "status": "new",
            "created_at": now,
            "updated_at": now,
            "created_by_agent_run": run_id,
        })
        insight_ids.append(iid)

    flag_ids: list[str] = []
    notif_ids: list[str] = []
    for fl in findings.get("flags", [])[:2]:
        # de-dupe: at most one open llm_anomaly flag at a time
        if await workspace_flags.find_one(
            {"workspace_id": ws, "flag_type": "llm_anomaly", "status": "open"}
        ):
            continue
        fid = str(uuid4())
        severity = "critical" if str(fl.get("severity")).lower() == "critical" else "warning"
        summary = fl.get("summary", "Anomaly detected in workspace activity.")
        await workspace_flags.insert_one({
            "_id": fid,
            "workspace_id": ws,
            "flag_type": "llm_anomaly",
            "detection": "llm",
            "severity": severity,
            "summary_persona": summary,
            "detail": fl.get("detail", {}) if isinstance(fl.get("detail"), dict) else {"note": fl.get("detail")},
            "metric": {"name": "llm_anomaly", "value": 1.0, "limit": 0.0},
            "langsmith_run_url": run_url,
            "status": "open",
            "notified": {"in_app": False, "email": False, "at": None},
            "created_at": now,
            "resolved_at": None,
        })
        flag_ids.append(fid)
        nid = await notify_mod.deliver(
            ws, kind="flag", source_id=fid, title="Workspace anomaly flagged",
            body_persona=summary, severity=severity,
        )
        notif_ids.append(nid)
        await workspace_flags.update_one(
            {"_id": fid},
            {"$set": {"notified": {"in_app": True, "email": severity == "critical", "at": now}}},
        )

    # notify on high-priority insights when the model asked for it
    if findings.get("notify"):
        for iid, ins in zip(insight_ids, findings.get("insights", [])):
            if ins.get("priority") == "high":
                nid = await notify_mod.deliver(
                    ws, kind="insight", source_id=iid,
                    title=(ins.get("title") or "Workspace recommendation")[:120],
                    body_persona=ins.get("body", ""), severity="warning", email=False,
                )
                notif_ids.append(nid)

    await agent_worker_state.update_one(
        {"_id": f"supervisor:{ws}"},
        {"$set": {"last_llm_pass_at": now, "events_since_last_llm": 0,
                  "event_buffer": [], "lock_until": None, "updated_at": now}},
        upsert=True,
    )

    logger.info(
        "supervisor persist: ws=%s insights=%d flags=%d notifications=%d trigger=%s",
        ws, len(insight_ids), len(flag_ids), len(notif_ids), state.get("trigger"),
    )
    return {"persisted": {
        "insight_ids": insight_ids, "flag_ids": flag_ids, "notification_ids": notif_ids,
    }}


# ─────────────────────────────────────────────────────────────────────────────
# Groq helper — tolerate SDKs that don't accept reasoning_effort
# ─────────────────────────────────────────────────────────────────────────────

async def _groq_chat(client, messages, *, tools=None, tool_choice=None,
                     max_tokens=1400, temperature=0.3):
    kwargs = dict(
        model=GroqModel.POWERFUL.value,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice or "auto"
    try:
        return await client.chat.completions.create(reasoning_effort="high", **kwargs)
    except TypeError:
        return await client.chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        # Some Groq builds reject reasoning_effort with a 400 rather than TypeError.
        if "reasoning_effort" in str(exc):
            return await client.chat.completions.create(**kwargs)
        raise
