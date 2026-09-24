"""
app/agents/text/event_emitter.py

EventEmitter — attached to LangGraph state.
Every node calls emit() to push human-readable events.
The SSE streaming endpoint reads from the queue and forwards to the client.

Usage inside any LangGraph node:
    await state["emitter"].emit_log("Analyzing source content — 847 words detected")
    await state["emitter"].emit_stage("source_analysis", "active")
    await state["emitter"].emit("output_started", {"platform": "linkedin", ...})
"""

import asyncio
from datetime import datetime, timezone
from typing import Any


class EventEmitter:
    """
    Async queue-based event emitter.
    Nodes push events → SSE endpoint reads them → client receives them.
    """

    # Sentinel value to signal the stream is complete
    DONE = object()

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self._resume_event: asyncio.Event = asyncio.Event()
        self._resume_choice: str | None = None
        # Run bookkeeping read after the pipeline finishes (Activity Log's
        # run summary) and while it runs (Control Tower progress). Plain
        # counters — the queue itself is drained by the SSE route.
        self.completed_platforms: list[str] = []
        self.stages: dict[str, str] = {}
        self.error: str | None = None
        # Optional async callback(emitter) fired after stage/output events —
        # the SSE route uses it to feed the Control Tower's run registry.
        self.on_progress = None

    # ─────────────────────────────────────────────────────────────────────────
    # Core emit
    # ─────────────────────────────────────────────────────────────────────────

    async def emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Push a typed SSE event to the queue."""
        if event_type == "output_complete" and data.get("platform"):
            self.completed_platforms.append(str(data["platform"]))
        elif event_type == "pipeline_stage" and data.get("stage"):
            self.stages[str(data["stage"])] = str(data.get("status", ""))
        elif event_type == "pipeline_error":
            self.error = str(data.get("message", ""))
        if self.on_progress and event_type in ("pipeline_stage", "output_complete"):
            try:
                await self.on_progress(self)
            except Exception:  # noqa: BLE001 — progress is best-effort
                pass
        await self.queue.put({
            "type":      event_type,
            "data":      data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # ─────────────────────────────────────────────────────────────────────────
    # Convenience helpers — used by narration.py
    # ─────────────────────────────────────────────────────────────────────────

    async def emit_log(
        self,
        message: str,
        level: str = "info",
    ) -> None:
        """Emit a human-readable activity log line."""
        await self.emit("activity_log", {
            "message": message,
            "level":   level,   # info | success | warning | error
        })

    async def emit_stage(self, stage: str, status: str) -> None:
        """
        Emit a pipeline stage status update.
        stage:  source_analysis | angle_extraction | hook_generation |
                platform_formatting | score_rank | approval_queue
        status: queued | active | complete | failed
        """
        await self.emit("pipeline_stage", {
            "stage":  stage,
            "status": status,
        })

    async def emit_output_started(
        self,
        platform:        str,
        agent_commentary: str = "",
        angle_used:      str = "",
    ) -> None:
        """Signal that the agent has started working on a specific platform."""
        await self.emit("output_started", {
            "platform":         platform,
            "agent_commentary": agent_commentary,
            "angle_used":       angle_used,
        })

    async def emit_content_chunk(self, platform: str, chunk: str) -> None:
        """Emit a partial content chunk for typewriter streaming."""
        await self.emit("content_chunk", {
            "platform": platform,
            "chunk":    chunk,
        })

    async def emit_output_complete(
        self,
        platform:          str,
        content:           str,
        hook_score:        int,
        readability_score: int,
        readability_level: str,
        agent_commentary:  str,
        decisions:         list[str],
        angle_used:        str,
        angle_score:       int,
        hook_version:      int,
        generation_time:   float,
        piece_id:          str,
        hashtags:          list[str] | None = None,
        hook_alternatives: list[str] | None = None,
        language:          str = "en",
        batch_day_index:   int | None = None,
    ) -> None:
        """Signal that a platform's output is fully complete and ready for approval.

        batch_day_index is only set for a batch-mode run (one platform,
        several days) — the frontend needs it to tell apart several cards
        that otherwise share the same platform, since a plain platform-only
        card identity (correct for every non-batch run) would collide.
        """
        await self.emit("output_complete", {
            "platform":          platform,
            "content":           content,
            "hook_score":        hook_score,
            "readability_score": readability_score,
            "readability_level": readability_level,
            "agent_commentary":  agent_commentary,
            "decisions":         decisions,
            "angle_used":        angle_used,
            "angle_score":       angle_score,
            "hook_version":      hook_version,
            "generation_time":   generation_time,
            "piece_id":          piece_id,
            "hashtags":          hashtags or [],
            "hook_alternatives": hook_alternatives or [],
            "language":          language,
            "batch_day_index":   batch_day_index,
        })

    async def emit_paused(
        self,
        platform: str,
        reason:   str,
        options:  list[dict],
    ) -> None:
        """
        Signal that the agent has paused and needs human input.
        options: [{"id": "angle_1", "label": "Problem framing", "score": 81}]
        """
        await self.emit("agent_paused", {
            "platform": platform,
            "reason":   reason,
            "options":  options,
        })

    async def emit_complete(self, session_id: str, total_pieces: int) -> None:
        """Signal that the entire pipeline run is complete."""
        await self.emit("pipeline_complete", {
            "session_id":   session_id,
            "total_pieces": total_pieces,
        })
        # Push the done sentinel so the SSE loop knows to stop
        await self.queue.put(self.DONE)

    async def emit_error(self, message: str, recoverable: bool = False) -> None:
        """Signal a pipeline error."""
        await self.emit("pipeline_error", {
            "message":     message,
            "recoverable": recoverable,
        })
        await self.queue.put(self.DONE)

    # ─────────────────────────────────────────────────────────────────────────
    # Human-in-the-loop pause/resume
    #
    # READY, NOT YET TRIGGERED (2026-09-24): no pipeline node calls
    # emit_paused()/wait_for_resume() today, so runs never pause. The rest is
    # live end to end — the resume endpoint (/pipeline/session/{id}/resume),
    # cross-instance relay (app.agents.text.session_relay) and the frontend's
    # agent_paused card — so a future "pick an angle" step only has to call
    # emit_paused() then wait_for_resume() from its node.
    # ─────────────────────────────────────────────────────────────────────────

    async def wait_for_resume(self, timeout: float = 30.0) -> str:
        """
        Block until the user resumes the pipeline by making a choice.
        Returns the chosen option ID.
        Raises asyncio.TimeoutError if user does not respond in time.
        """
        self._resume_event.clear()
        self._resume_choice = None
        await asyncio.wait_for(self._resume_event.wait(), timeout=timeout)
        return self._resume_choice or ""

    async def resume(self, choice: str) -> None:
        """Called by the resume API endpoint to unblock wait_for_resume."""
        self._resume_choice = choice
        self._resume_event.set()