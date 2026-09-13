"""
Generate 3 alternative opening hooks per content piece.
Maps to extras.hookVariations toggle in ConfigPanel ExtrasToggles.
Only runs when hookVariations is True.
"""

import logging
from app.models.text import AgentTask, AgentResult
from app.prompts.registry import load_prompt
from app.shared.llm import call_llm_structured

logger = logging.getLogger(__name__)


async def run_hook_agent(task: AgentTask) -> AgentResult:
    """
    Generate 3 hook variants using three structurally different approaches.
    Score each 1-10 for scroll-stopping power.
    Return the recommended index (highest score).
    """
    platform_label = task.platform.value if task.platform else "social media"

    prompt = load_prompt(
        "text/hooks/generate",
        brand_context=task.brand_context,
        platform_label=platform_label,
        banned_openings=task.metadata.get("banned_openings", []),
        banned_words=task.metadata.get("banned_words", []),
        content=task.content[:600],
    )

    result = await call_llm_structured(prompt)

    if not result or "hooks" not in result:
        logger.warning(
            "Hook agent failed for session %s — returning empty hooks",
            task.session_id,
        )
        return AgentResult(
            agent="hook",
            platform=task.platform,
            output={"hooks": [], "recommended": 0},
            success=False,
        )

    return AgentResult(
        agent="hook",
        platform=task.platform,
        output=result,
        success=True,
    )


def apply_recommended_hook(content: str, hooks: list[dict], recommended_index: int) -> str:
    """
    Replace the opening line of generated content with the recommended hook.
    Finds the first non-empty line and replaces it.
    """
    if not hooks or recommended_index >= len(hooks):
        return content

    recommended = hooks[recommended_index].get("text", "")
    if not recommended:
        return content

    lines = content.strip().split("\n")
    for i, line in enumerate(lines):
        if line.strip():
            lines[i] = recommended
            break

    return "\n".join(lines)