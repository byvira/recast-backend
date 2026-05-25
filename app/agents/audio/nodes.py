"""Nodes for the audio pipeline LangGraph agent."""

from datetime import datetime
from app.agents.base import BaseAgentState
from app.utils.brand import build_brand_context
from app.utils.llm import call_llm, call_llm_structured, transcribe_audio, GroqModel


async def plan_node(state: BaseAgentState) -> dict:
    """
    Build the execution plan for the audio processing pipeline.
    Uses call_llm_structured to produce a list of processing steps.
    """
    brand_ctx = build_brand_context(state["brand"], "audio")
    raw = state["raw_input"]
    title = raw.get("title", "") if isinstance(raw, dict) else str(raw)

    prompt = (
        f"Create a step-by-step audio content plan for: {title}\n"
        "Return JSON with key 'steps' as an array of short action strings."
    )
    result = await call_llm_structured(prompt=prompt, system=brand_ctx)
    plan = result.get("steps", ["transcribe", "analyse", "generate", "evaluate", "deliver"])

    return {
        "plan": plan,
        "current_step": "transcribe",
    }


async def transcribe_node(state: BaseAgentState) -> dict:
    """
    Transcribe audio input using Groq Whisper.
    Falls back to provided transcript text if no file_path given.
    """
    raw = state["raw_input"]
    file_path = raw.get("file_path", "") if isinstance(raw, dict) else ""
    language = raw.get("language", "en") if isinstance(raw, dict) else "en"

    if file_path:
        transcript = await transcribe_audio(file_path=file_path, language=language)
    else:
        transcript = {
            "text": raw.get("transcript", "") if isinstance(raw, dict) else "",
            "segments": [],
        }

    return {
        "intermediate_outputs": {
            **state["intermediate_outputs"],
            "transcript": transcript,
        },
        "current_step": "analyse",
        "tool_calls": [
            *state["tool_calls"],
            {"node": "transcribe", "model": "whisper-large-v3"},
        ],
    }


async def analyse_node(state: BaseAgentState) -> dict:
    """
    Analyse the audio transcript for key topics, timestamps, and brand alignment.
    Stores structured analysis in intermediate_outputs.
    """
    brand_ctx = build_brand_context(state["brand"], "audio")
    transcript = state["intermediate_outputs"].get("transcript", {})
    text = transcript.get("text", "")
    raw = state["raw_input"]
    output_type = raw.get("output_type", "show_notes") if isinstance(raw, dict) else "show_notes"

    prompt = (
        f"Analyse this audio transcript for {output_type} generation:\n\n{text[:3000]}\n\n"
        "Return JSON with keys: 'topics' (list), 'key_quotes' (list), "
        "'chapter_markers' (list of {time, title}), 'sentiment' (str)."
    )
    analysis = await call_llm_structured(prompt=prompt, system=brand_ctx)

    return {
        "intermediate_outputs": {**state["intermediate_outputs"], "analysis": analysis},
        "current_step": "generate",
    }


async def generate_node(state: BaseAgentState) -> dict:
    """
    Generate audio content output (show notes, titles, captions) from the transcript.
    Injects brand context and retry feedback into the prompt.
    """
    brand_ctx = build_brand_context(state["brand"], "audio")
    transcript = state["intermediate_outputs"].get("transcript", {})
    analysis = state["intermediate_outputs"].get("analysis", {})
    raw = state["raw_input"]
    output_type = raw.get("output_type", "show_notes") if isinstance(raw, dict) else "show_notes"
    retry = state["retry_count"]

    quality_note = (
        "\nIMPORTANT: Previous attempt was below quality threshold. Improve depth and engagement."
    ) if retry > 0 else ""

    prompt = (
        f"Generate {output_type} for this audio content:\n"
        f"Topics: {analysis.get('topics', [])}\n"
        f"Key quotes: {analysis.get('key_quotes', [])}\n"
        f"Full transcript excerpt: {transcript.get('text', '')[:2000]}"
        f"{quality_note}"
    )
    content = await call_llm(prompt=prompt, system=brand_ctx, model=GroqModel.BALANCED)

    return {
        "intermediate_outputs": {
            **state["intermediate_outputs"],
            "generated_content": content,
        },
        "current_step": "evaluate",
        "tool_calls": [
            *state["tool_calls"],
            {"node": "generate", "model": GroqModel.BALANCED.value, "retry": retry},
        ],
    }


async def evaluate_node(state: BaseAgentState) -> dict:
    """
    Score the generated audio content for quality and brand alignment.
    Stores scores in quality_scores for the routing decision.
    """
    brand_ctx = build_brand_context(state["brand"], "audio")
    content = state["intermediate_outputs"].get("generated_content", "")
    brand_name = state["brand"].get("name", "")

    prompt = (
        f"Evaluate this audio content output for brand '{brand_name}':\n\n{content}\n\n"
        "Return JSON with keys: 'completeness' (0-1), 'brand_alignment' (0-1), "
        "'accuracy' (0-1), 'overall' (0-1)."
    )
    scores_raw = await call_llm_structured(prompt=prompt, system=brand_ctx)

    quality_scores = {
        "completeness": float(scores_raw.get("completeness", 0.5)),
        "brand_alignment": float(scores_raw.get("brand_alignment", 0.5)),
        "accuracy": float(scores_raw.get("accuracy", 0.5)),
        "content": float(scores_raw.get("overall", 0.5)),
    }

    return {
        "quality_scores": quality_scores,
        "current_step": "evaluate",
    }


async def retry_node(state: BaseAgentState) -> dict:
    """
    Increment retry counter and prepare for another generation attempt.
    Called when quality scores fall below the 0.75 threshold.
    """
    return {
        "retry_count": state["retry_count"] + 1,
        "current_step": "generate",
        "errors": [
            *state["errors"],
            f"Audio content quality below threshold on attempt {state['retry_count'] + 1}. Retrying.",
        ],
    }


async def deliver_node(state: BaseAgentState) -> dict:
    """
    Package the final audio content output and mark the run as completed.
    Sets status to 'completed' with completion timestamp.
    """
    raw = state["raw_input"]
    output_type = raw.get("output_type", "show_notes") if isinstance(raw, dict) else "show_notes"
    transcript = state["intermediate_outputs"].get("transcript", {})

    return {
        "outputs": {
            **state["outputs"],
            "content": state["intermediate_outputs"].get("generated_content", ""),
            "output_type": output_type,
            "transcript": transcript.get("text", ""),
            "segments": transcript.get("segments", []),
            "quality_scores": state["quality_scores"],
        },
        "status": "completed",
        "completed_at": datetime.utcnow().isoformat(),
        "current_step": "deliver",
    }
