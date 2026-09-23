"""
Generate 3 alternative opening hooks per content piece.
Maps to extras.hookVariations toggle in ConfigPanel ExtrasToggles.
Only runs when hookVariations is True.
"""

import logging
from app.models.text import AgentTask, AgentResult
from app.pipelines.text.generator import GENERIC_OPENINGS
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


def _starts_with_generic_opening(text: str) -> bool:
    lowered = text.strip().lower()
    return any(lowered.startswith(g) for g in GENERIC_OPENINGS)


def apply_recommended_hook(content: str, hooks: list[dict], recommended_index: int) -> str:
    """
    Replace the opening line of generated content with the best hook that
    ISN'T a banned generic opener — not blindly whichever the model scored
    highest. The model's own self-scoring has no visibility into
    GENERIC_OPENINGS and will happily rate a banned-phrase hook a 9/10, so
    trusting recommended_index alone let already-banned openers (e.g. "the
    uncomfortable truth", one of exactly 3 hook styles text/hooks/generate.jinja
    always offers) back into real output after generate_node's own validated
    content had already avoided them. Falls back to the original content if
    every candidate hook is generic, rather than forcing a bad swap.
    """
    if not hooks:
        return content

    ranked = sorted(
        range(len(hooks)),
        key=lambda i: hooks[i].get("score", 0) if isinstance(hooks[i], dict) else 0,
        reverse=True,
    )
    # Try the model's actual recommendation first, then fall back through the
    # rest by score — only skipping a candidate if it's a banned generic opener.
    candidate_order = [recommended_index] + [i for i in ranked if i != recommended_index]

    recommended = ""
    for i in candidate_order:
        if i < 0 or i >= len(hooks):
            continue
        text = hooks[i].get("text", "") if isinstance(hooks[i], dict) else ""
        if text and not _starts_with_generic_opening(text):
            recommended = text
            break

    if not recommended:
        logger.warning("All hook candidates were generic openers — keeping original opening line")
        return content

    lines = content.strip().split("\n")
    for i, line in enumerate(lines):
        if line.strip():
            lines[i] = recommended
            break

    return "\n".join(lines)