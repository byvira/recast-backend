"""Connects the real, previously-disconnected audio LangGraph agent
(app.agents.audio.graph) — same import pattern
app.pipelines.text.orchestrator uses for app.agents.text.graph, not
app.agents.__init__ (that module's own comment forbids agent-graph imports
there, to avoid circular import chains — see
pow/audio_image_pipeline/PROGRESS.md's Stage 1 correction note).

Real correction from the plan (pow/audio_image_pipeline/GAPS.md G-7): this
agent's plan -> transcribe -> analyse -> generate -> evaluate -> deliver
graph takes audio-or-transcript in and produces derived TEXT (show notes/
captions/analysis) out — it is Audio's transcript-analysis/derivative
generator, NOT a script-to-synthesized-audio pipeline. Stage 4's real
narration flow (script -> TTS -> AudioAsset) does not call this — it calls
app.pipelines.media.tts_generation.synthesize_speech directly. This module
exists so the agent is genuinely connected and callable for its real job
(Phase 2's transcription-in / Phase 5's show-notes derivatives), not left
disconnected — "connect and extend it, don't build a parallel simpler
flow" still holds, just for the job this agent actually does.
"""

from typing import Any, Optional

from app.agents.audio.graph import audio_agent
from app.agents.base import initial_state

_RUN_NAME = "audio_analysis_pipeline"


async def run_audio_analysis(
    *,
    user_id: str,
    workspace_id: str,
    brand: dict,
    raw_input: dict[str, Any],
    max_retries: int = 2,
) -> dict[str, Any]:
    """Runs the real audio agent end to end (plan/transcribe/analyse/
    generate/evaluate/retry/deliver) and returns its `outputs` dict
    (content/output_type/transcript/segments/quality_scores — see
    app.agents.audio.nodes.deliver_node).

    `raw_input` matches what transcribe_node reads: `file_path` (real
    audio to transcribe via Groq Whisper) or `transcript` (already-have-
    text path) plus `title`/`language`/`output_type`. Never raises for a
    tracing reason (ainvoke_traced falls back to untraced on failure); a
    real node failure inside the graph still propagates, same as the text
    pipeline's own orchestrator.
    """
    from app.core.tracing import ainvoke_traced

    state = initial_state(user_id=user_id, brand=brand, raw_input=raw_input, max_retries=max_retries)
    final_state, _trace_url = await ainvoke_traced(
        audio_agent,
        state,
        run_name=_RUN_NAME,
        agent="audio_analysis",
        workspace_id=workspace_id,
        user_id=user_id,
    )
    return final_state["outputs"]
