"""Nodes for the image pipeline LangGraph agent."""

import json
from datetime import datetime
from app.agents.base import BaseAgentState
from app.utils.brand import build_brand_context
from app.utils.llm import (
    call_llm_structured,
    call_vision,
    enhance_image_prompt,
    generate_image,
)


async def plan_node(state: BaseAgentState) -> dict:
    """
    Build the execution plan for the image generation pipeline.
    Uses call_llm_structured to produce a list of generation steps.
    """
    brand_ctx = build_brand_context(state["brand"], "image")
    raw = state["raw_input"]
    concept = raw.get("prompt", "") if isinstance(raw, dict) else str(raw)

    prompt = (
        f"Create an image generation plan for: {concept}\n"
        "Return JSON with key 'steps' as an array of short action strings."
    )
    result = await call_llm_structured(prompt=prompt, system=brand_ctx)
    plan = result.get("steps", ["analyse", "generate", "evaluate", "deliver"])

    return {
        "plan": plan,
        "current_step": "analyse",
    }


async def analyse_node(state: BaseAgentState) -> dict:
    """
    Analyse the image brief and extract visual requirements, brand colors, and style.
    Handles both text prompts and image uploads via call_vision.
    """
    brand_ctx = build_brand_context(state["brand"], "image")
    raw = state["raw_input"]
    concept = raw.get("prompt", "") if isinstance(raw, dict) else str(raw)
    image_bytes: bytes = raw.get("image_bytes", b"") if isinstance(raw, dict) else b""

    if image_bytes:
        vision_result = await call_vision(
            prompt="Describe this image: extract style, colors, composition, and brand elements.",
            image_bytes=image_bytes,
        )
        analysis_text = vision_result
    else:
        vision_result = ""
        analysis_text = concept

    prompt = (
        f"Analyse this image brief for brand image generation:\n{analysis_text}\n\n"
        "Return JSON with keys: 'visual_style' (str), 'color_palette' (list), "
        "'composition' (str), 'mood' (str), 'brand_elements' (list)."
    )
    analysis = await call_llm_structured(prompt=prompt, system=brand_ctx)
    if vision_result:
        analysis["vision_description"] = vision_result

    return {
        "intermediate_outputs": {**state["intermediate_outputs"], "analysis": analysis},
        "current_step": "generate",
    }


async def generate_node(state: BaseAgentState) -> dict:
    """
    Enhance the prompt with brand identity and generate the image.
    Uses enhance_image_prompt then generate_image (both stubs until provider wired).
    """
    raw = state["raw_input"]
    concept = raw.get("prompt", "") if isinstance(raw, dict) else str(raw)
    analysis = state["intermediate_outputs"].get("analysis", {})
    brand = state["brand"]
    retry = state["retry_count"]

    brand_colors = ", ".join(brand.get("colors", []))
    brand_style = analysis.get("visual_style", brand.get("voice", {}).get("style", ""))
    brand_aesthetic = analysis.get("mood", "")

    if retry > 0:
        concept = f"{concept} — highly detailed, professional, vivid"

    enhanced_prompt = await enhance_image_prompt(
        raw_prompt=concept,
        brand_colors=brand_colors,
        brand_style=brand_style,
        brand_aesthetic=brand_aesthetic,
    )

    image_bytes = await generate_image(prompt=enhanced_prompt)

    return {
        "intermediate_outputs": {
            **state["intermediate_outputs"],
            "enhanced_prompt": enhanced_prompt,
            "image_bytes": image_bytes,
        },
        "current_step": "evaluate",
        "tool_calls": [
            *state["tool_calls"],
            {"node": "generate", "prompt": enhanced_prompt, "retry": retry},
        ],
    }


async def evaluate_node(state: BaseAgentState) -> dict:
    """
    Evaluate image quality by analysing the generated image with Gemini Vision.
    Falls back to prompt-based scoring when no image bytes are available.
    """
    brand_ctx = build_brand_context(state["brand"], "image")
    image_bytes: bytes = state["intermediate_outputs"].get("image_bytes", b"")
    enhanced_prompt = state["intermediate_outputs"].get("enhanced_prompt", "")
    brand_name = state["brand"].get("name", "")

    if image_bytes:
        vision_eval = await call_vision(
            prompt=(
                f"Evaluate this image for brand '{brand_name}'. "
                "Rate brand alignment, visual quality, composition, and mood match (0-1 each). "
                "Return only JSON."
            ),
            image_bytes=image_bytes,
        )
        try:
            scores_raw = json.loads(vision_eval)
        except (json.JSONDecodeError, TypeError):
            scores_raw = {}
    else:
        prompt = (
            f"Evaluate this image prompt for brand '{brand_name}':\n{enhanced_prompt}\n\n"
            "Return JSON with keys: 'brand_alignment' (0-1), 'visual_quality' (0-1), "
            "'composition' (0-1), 'overall' (0-1)."
        )
        scores_raw = await call_llm_structured(prompt=prompt, system=brand_ctx)

    quality_scores = {
        "brand_alignment": float(scores_raw.get("brand_alignment", 0.8)),
        "visual_quality": float(scores_raw.get("visual_quality", 0.8)),
        "composition": float(scores_raw.get("composition", 0.8)),
        "content": float(scores_raw.get("overall", 0.8)),
    }

    return {
        "quality_scores": quality_scores,
        "current_step": "evaluate",
    }


async def retry_node(state: BaseAgentState) -> dict:
    """
    Increment retry counter and prepare for another image generation attempt.
    Called when quality scores fall below the 0.75 threshold.
    """
    return {
        "retry_count": state["retry_count"] + 1,
        "current_step": "generate",
        "errors": [
            *state["errors"],
            f"Image quality below threshold on attempt {state['retry_count'] + 1}. Retrying.",
        ],
    }


async def deliver_node(state: BaseAgentState) -> dict:
    """
    Package the final image and metadata into outputs and mark as completed.
    Sets status to 'completed' with completion timestamp.
    """
    raw = state["raw_input"]

    return {
        "outputs": {
            **state["outputs"],
            "image_bytes": state["intermediate_outputs"].get("image_bytes", b""),
            "enhanced_prompt": state["intermediate_outputs"].get("enhanced_prompt", ""),
            "analysis": state["intermediate_outputs"].get("analysis", {}),
            "quality_scores": state["quality_scores"],
            "original_prompt": raw.get("prompt", "") if isinstance(raw, dict) else str(raw),
        },
        "status": "completed",
        "completed_at": datetime.utcnow().isoformat(),
        "current_step": "deliver",
    }
