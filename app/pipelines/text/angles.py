"""
Generate 3 genuinely distinct strategic angles on existing content.

Feature 8 — Library's "Repurpose with 3 Fresh Angles" button opened the
generic cross-platform repurpose modal, not an actual angle comparison —
there was no real angle-extraction anywhere in the backend (the old
angle_used/angle_score fields on generated pieces were hardcoded
"auto"/0 placeholders). This is that real capability: not 3 tone
variations of the same content, but 3 different choices of what to lead
with and emphasize, each a full rewrite.
"""

import logging

from app.models.text import AgentTask, AgentResult
from app.pipelines.text.generator import build_language_instruction
from app.prompts.registry import load_prompt
from app.shared.language import detect_language
from app.shared.llm import call_llm_structured

logger = logging.getLogger(__name__)


async def run_angles_agent(task: AgentTask) -> AgentResult:
    """
    Generate 3 angle variants using three structurally different approaches.
    Each variant is a full rewrite, not a snippet — see the prompt for the
    exact angle definitions (contrarian / personal story / concrete outcome).
    """
    platform_label = task.platform.value if task.platform else "social media"

    # Angles rewrite *existing* content, so the source content's own
    # language should win outright — unlike fresh generation (which
    # resolves a request/workspace/user default), there's no "default" here
    # that should ever override what the user actually wrote. Without this,
    # every prompt in this file was English-only regardless of input, and
    # Tamil (or any non-English) source content came back rewritten in
    # English every time.
    detected = detect_language(task.content)
    language_instruction = build_language_instruction(detected or "en")

    prompt = load_prompt(
        "text/angles/generate",
        brand_context=task.brand_context,
        platform_label=platform_label,
        banned_words=task.metadata.get("banned_words", []),
        content=task.content[:4000],
        language_instruction=language_instruction,
    )

    # Three full-length rewrites in one JSON response need real headroom —
    # the 2500 default (sized for a single hook-length response) would
    # likely truncate mid-JSON on a long platform like Blog or Newsletter.
    result = await call_llm_structured(prompt, max_tokens=5000)

    if not result or "angles" not in result or len(result.get("angles", [])) == 0:
        logger.warning(
            "Angles agent failed for session %s — returning empty angles",
            task.session_id,
        )
        return AgentResult(
            agent="angles",
            platform=task.platform,
            output={"angles": []},
            success=False,
        )

    return AgentResult(
        agent="angles",
        platform=task.platform,
        output=result,
        success=True,
    )
