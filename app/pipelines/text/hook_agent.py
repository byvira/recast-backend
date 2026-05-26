"""
Generate 3 alternative opening hooks per content piece.
Maps to extras.hookVariations toggle in ConfigPanel ExtrasToggles.
Only runs when hookVariations is True.
"""

import logging
from app.models.text import AgentTask, AgentResult
from app.shared.llm import call_llm_structured

logger = logging.getLogger(__name__)


async def run_hook_agent(task: AgentTask) -> AgentResult:
    """
    Generate 3 hook variants using three structurally different approaches.
    Score each 1-10 for scroll-stopping power.
    Return the recommended index (highest score).
    """
    platform_label = task.platform.value if task.platform else "social media"

    # ── Banned openings block ─────────────────────────────────────────────
    banned_openings = task.metadata.get("banned_openings", [])
    banned_openings_block = ""
    if banned_openings:
        banned_openings_block = "\nBANNED HOOK OPENINGS — never start any hook with these:\n"
        for opening in banned_openings:
            banned_openings_block += f"  ✗ \"{opening}\"\n"
        banned_openings_block += "\n"

    # ── Banned hashtag vocabulary block ───────────────────────────────────
    banned_words = task.metadata.get("banned_words", [])
    banned_hashtag_block = ""
    if banned_words:
        banned_vocab = ", ".join(f"#{w.replace(' ', '')}" for w in banned_words)
        banned_hashtag_block = (
            f"\nBANNED HASHTAG VOCABULARY: {banned_vocab}\n"
            f"Never use these or close variations as hashtags.\n"
        )

    prompt = f"""
{task.brand_context}

Generate exactly 3 alternative opening hooks for the content below.
Each hook must use a distinctly different structural approach.

Hook 1 — Contrarian: Challenge a common belief or assumption the audience holds.
Hook 2 — Specific outcome: Lead with a concrete number, result, timeframe, or outcome.
Hook 3 — Uncomfortable truth: State an observation the audience feels but nobody says out loud.

Rules for all hooks:
- Maximum 2 sentences each
- Must match the brand voice exactly
- Must be suitable for {platform_label}
- Score each 1-10 for scroll-stopping power (be honest — most hooks are 5-7)
- Use only specific details from the brand story — never invented statistics
{banned_openings_block}{banned_hashtag_block}
CONTENT:
{task.content[:600]}

Return valid JSON only:
{{
  "hooks": [
    {{"text": "...", "style": "Contrarian", "score": 8}},
    {{"text": "...", "style": "Specific outcome", "score": 7}},
    {{"text": "...", "style": "Uncomfortable truth", "score": 9}}
  ],
  "recommended": 2
}}

The recommended field is the index (0, 1, or 2) of the highest scoring hook.
"""

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