"""Nodes for the video pipeline LangGraph agent."""

from datetime import datetime
from app.agents.base import BaseAgentState
from app.utils.brand import build_brand_context
from app.utils.llm import call_llm, call_llm_structured, transcribe_audio, GroqModel


async def plan_node(state: BaseAgentState) -> dict:
    """
    Build the execution plan for the video processing pipeline.
    Uses call_llm_structured to produce a list of processing steps.
    """
    brand_ctx = build_brand_context(state["brand"], "video")
    raw = state["raw_input"]
    title = raw.get("title", "") if isinstance(raw, dict) else str(raw)
    output_type = raw.get("output_type", "repurpose") if isinstance(raw, dict) else "repurpose"

    prompt = (
        f"Create a video content plan for '{title}' (goal: {output_type}).\n"
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
    Transcribe the video audio track using Groq Whisper.
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
    Analyse video transcript for clips, highlights, topics, and repurposing opportunities.
    Stores structured analysis in intermediate_outputs.
    """
    brand_ctx = build_brand_context(state["brand"], "video")
    transcript = state["intermediate_outputs"].get("transcript", {})
    text = transcript.get("text", "")
    raw = state["raw_input"]
    output_type = raw.get("output_type", "repurpose") if isinstance(raw, dict) else "repurpose"

    prompt = (
        f"Analyse this video transcript for {output_type}:\n\n{text[:4000]}\n\n"
        "Return JSON with keys: 'key_moments' (list of {time, text, value}), "
        "'topics' (list), 'clip_suggestions' (list of {start, end, reason}), "
        "'content_type' (str), 'repurpose_angles' (list)."
    )
    analysis = await call_llm_structured(prompt=prompt, system=brand_ctx)

    return {
        "intermediate_outputs": {**state["intermediate_outputs"], "analysis": analysis},
        "current_step": "generate",
    }


async def generate_node(state: BaseAgentState) -> dict:
    """
    Generate repurposed video content (scripts, captions, social posts) from analysis.
    Injects brand context, platform target, and retry feedback into the prompt.
    """
    brand_ctx = build_brand_context(state["brand"], "video")
    transcript = state["intermediate_outputs"].get("transcript", {})
    analysis = state["intermediate_outputs"].get("analysis", {})
    raw = state["raw_input"]
    output_type = raw.get("output_type", "repurpose") if isinstance(raw, dict) else "repurpose"
    platform = raw.get("platform", "general") if isinstance(raw, dict) else "general"
    retry = state["retry_count"]

    quality_note = (
        "\nIMPORTANT: Previous attempt scored below quality threshold. "
        "Improve hooks, structure, and platform optimization."
    ) if retry > 0 else ""

    prompt = (
        f"Generate {output_type} content for {platform} from this video:\n"
        f"Key moments: {analysis.get('key_moments', [])}\n"
        f"Topics: {analysis.get('topics', [])}\n"
        f"Repurpose angles: {analysis.get('repurpose_angles', [])}\n"
        f"Transcript excerpt: {transcript.get('text', '')[:2000]}"
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
    Score the generated video content for quality, platform fit, and brand alignment.
    Stores scores in quality_scores for the routing decision.
    """
    brand_ctx = build_brand_context(state["brand"], "video")
    content = state["intermediate_outputs"].get("generated_content", "")
    raw = state["raw_input"]
    platform = raw.get("platform", "general") if isinstance(raw, dict) else "general"
    brand_name = state["brand"].get("name", "")

    prompt = (
        f"Evaluate this video content for brand '{brand_name}' on {platform}:\n\n{content}\n\n"
        "Return JSON with keys: 'platform_fit' (0-1), 'brand_alignment' (0-1), "
        "'engagement_potential' (0-1), 'overall' (0-1)."
    )
    scores_raw = await call_llm_structured(prompt=prompt, system=brand_ctx)

    quality_scores = {
        "platform_fit": float(scores_raw.get("platform_fit", 0.5)),
        "brand_alignment": float(scores_raw.get("brand_alignment", 0.5)),
        "engagement_potential": float(scores_raw.get("engagement_potential", 0.5)),
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
            f"Video content quality below threshold on attempt {state['retry_count'] + 1}. Retrying.",
        ],
    }


async def deliver_node(state: BaseAgentState) -> dict:
    """
    Package the final video content and metadata into outputs and mark as completed.
    Sets status to 'completed' with completion timestamp.
    """
    raw = state["raw_input"]
    transcript = state["intermediate_outputs"].get("transcript", {})
    analysis = state["intermediate_outputs"].get("analysis", {})

    return {
        "outputs": {
            **state["outputs"],
            "content": state["intermediate_outputs"].get("generated_content", ""),
            "output_type": raw.get("output_type", "repurpose") if isinstance(raw, dict) else "repurpose",
            "platform": raw.get("platform", "general") if isinstance(raw, dict) else "general",
            "transcript": transcript.get("text", ""),
            "segments": transcript.get("segments", []),
            "clip_suggestions": analysis.get("clip_suggestions", []),
            "quality_scores": state["quality_scores"],
        },
        "status": "completed",
        "completed_at": datetime.utcnow().isoformat(),
        "current_step": "deliver",
    }
