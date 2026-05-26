import logging
from app.models.text import AgentTask, AgentResult, Platform
from app.pipelines.text.brand_context import build_goal_context, build_tone_override
from app.pipelines.text.generator import PLATFORM_RULES

from app.shared.llm import call_llm_structured

logger = logging.getLogger(__name__)



REPURPOSE_INSTRUCTIONS = {
    (Platform.BLOG, Platform.LINKEDIN): "Extract the single strongest insight. Rewrite as a hook-led LinkedIn post. Do not summarise — find the most shareable angle.",
    (Platform.BLOG, Platform.TWITTER_THREAD): "Break this blog into a 7-tweet thread. Tweet 1 is a standalone hook. Each middle tweet covers one key point. Final tweet is a CTA.",
    (Platform.BLOG, Platform.NEWSLETTER): "Rewrite as a conversational newsletter section. Remove formal blog structure. Add a personal angle and one concrete takeaway.",
    (Platform.BLOG, Platform.INSTAGRAM): "Extract the strongest insight. Rewrite as an Instagram caption with a hook first line, 100-130 words, CTA, and hashtags.",
(Platform.LINKEDIN, Platform.TWITTER): (
    "Rewrite as a Twitter/X tweet. "
    "HARD REQUIREMENT: minimum 200 characters, maximum 280 characters. "
    "Count characters before outputting — if under 200, add a specific detail, outcome, or follow-up thought. "
    "One complete punchy idea. Hook first. Do not just compress — expand the idea if needed."
),
    (Platform.LINKEDIN, Platform.TWITTER_THREAD): "Expand this LinkedIn post into a 7-tweet thread. Each tweet unpacks one idea from the post.",
    (Platform.LINKEDIN, Platform.INSTAGRAM): "Rewrite as an Instagram caption. More conversational. Add a hook line and hashtags.",
    (Platform.LINKEDIN, Platform.BLOG): "Expand this LinkedIn post into a full blog post. Add depth, examples, and structure.",
    (Platform.NEWSLETTER, Platform.LINKEDIN): "Extract the main insight. Rewrite as a hook-led LinkedIn post.",
    (Platform.NEWSLETTER, Platform.TWITTER_THREAD): "Convert to a Twitter thread. Each key point becomes one tweet.",
    (Platform.TWITTER_THREAD, Platform.BLOG): "Expand this thread into a full blog post. Each tweet becomes a section with full detail.",
    (Platform.TWITTER_THREAD, Platform.LINKEDIN): "Compress the thread's core argument into a single powerful LinkedIn post.",
}

FALLBACK_INSTRUCTION = "Adapt this content for {target}. Preserve the core message. Rewrite completely for the target platform's format, tone, and audience expectations."


import logging
from app.models.text import AgentTask, AgentResult, Platform
from app.pipelines.text.brand_context import build_goal_context, build_tone_override
from app.pipelines.text.generator import (
    PLATFORM_RULES,
    build_approved_copy_instruction,
    build_banned_words_instruction,
)
from app.shared.llm import call_llm_structured

logger = logging.getLogger(__name__)


REPURPOSE_INSTRUCTIONS = {
    (Platform.BLOG, Platform.LINKEDIN): "Extract the single strongest insight. Rewrite as a hook-led LinkedIn post. Do not summarise — find the most shareable angle.",
    (Platform.BLOG, Platform.TWITTER_THREAD): "Break this blog into a 7-tweet thread. Tweet 1 is a standalone hook. Each middle tweet covers one key point. Final tweet is a CTA.",
    (Platform.BLOG, Platform.NEWSLETTER): "Rewrite as a conversational newsletter section. Remove formal blog structure. Add a personal angle and one concrete takeaway.",
    (Platform.BLOG, Platform.INSTAGRAM): "Extract the strongest insight. Rewrite as an Instagram caption with a hook first line, 100-130 words, CTA, and hashtags.",
    (Platform.LINKEDIN, Platform.TWITTER): (
        "Rewrite as a Twitter/X tweet. "
        "HARD REQUIREMENT: minimum 200 characters, maximum 280 characters. "
        "Count characters before outputting — if under 200, add a specific detail, outcome, or follow-up thought. "
        "One complete punchy idea. Hook first. Do not just compress — expand the idea if needed."
    ),
    (Platform.LINKEDIN, Platform.TWITTER_THREAD): "Expand this LinkedIn post into a 7-tweet thread. Each tweet unpacks one idea from the post.",
    (Platform.LINKEDIN, Platform.INSTAGRAM): "Rewrite as an Instagram caption. More conversational. Add a hook line and hashtags.",
    (Platform.LINKEDIN, Platform.BLOG): "Expand this LinkedIn post into a full blog post. Add depth, examples, and structure.",
    (Platform.NEWSLETTER, Platform.LINKEDIN): "Extract the main insight. Rewrite as a hook-led LinkedIn post.",
    (Platform.NEWSLETTER, Platform.TWITTER_THREAD): "Convert to a Twitter thread. Each key point becomes one tweet.",
    (Platform.TWITTER_THREAD, Platform.BLOG): "Expand this thread into a full blog post. Each tweet becomes a section with full detail.",
    (Platform.TWITTER_THREAD, Platform.LINKEDIN): "Compress the thread's core argument into a single powerful LinkedIn post.",
}

FALLBACK_INSTRUCTION = "Adapt this content for {target}. Preserve the core message. Rewrite completely for the target platform's format, tone, and audience expectations."


async def run_repurpose_agent(task: AgentTask, source_platform: Platform) -> AgentResult:
    instruction = REPURPOSE_INSTRUCTIONS.get(
        (source_platform, task.platform),
        FALLBACK_INSTRUCTION.format(target=task.platform.value),
    )

    goal_context = build_goal_context(task.metadata.get("goal"))
    tone_override = build_tone_override(task.metadata.get("tone"))
    platform_rules = PLATFORM_RULES.get(task.platform, "")

    # ── Enforcement data from metadata ────────────────────────────────────
    banned_words = task.metadata.get("banned_words", [])
    required_phrases = task.metadata.get("required_phrases", [])
    approved_openers = task.metadata.get("approved_openers", [])
    approved_closers = task.metadata.get("approved_closers", [])
    preferred_synonyms = task.metadata.get("preferred_synonyms", [])

    # ── Build enforcement instruction blocks ──────────────────────────────
    approved_copy_instruction = build_approved_copy_instruction(task)
    banned_instruction = build_banned_words_instruction(banned_words, preferred_synonyms)

    # ── Retry feedback block ──────────────────────────────────────────────
    retry_feedback = task.metadata.get("retry_feedback", "")
    retry_count = task.retry_count or 0
    retry_block = ""
    if retry_feedback and retry_count > 0:
        retry_block = (
            f"\n⚠️ PREVIOUS ATTEMPT FAILED — FIX THESE ISSUES:\n"
            f"{retry_feedback}\n"
            f"Every issue above must be resolved in this generation.\n"
        )

    # ── Build prompt — always built regardless of retry state ────────────
    prompt = f"""
REPURPOSING TASK: {instruction}
{retry_block}
SOURCE PLATFORM: {source_platform.value}

SOURCE CONTENT:
{task.content}

{platform_rules}

{approved_copy_instruction}

ADDITIONAL RULES:
{goal_context}
{tone_override}
{banned_instruction}

━━━ BRAND VOICE — apply every rule below to the output ━━━
{task.brand_context}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CRITICAL: Do not copy sentences from the source. Rewrite everything in the brand voice above.
Output must feel completely native to {task.platform.value}.
Zero banned words. Use approved vocabulary only.

Return valid JSON:
{{
  "content": "...",
  "word_count": 0,
  "char_count": 0,
  "platform": "{task.platform.value}"
}}
"""

    result = await call_llm_structured(prompt)

    if not result or "content" not in result:
        return AgentResult(
            agent="repurpose", platform=task.platform, output={}, success=False
        )

    content_str = result.get("content", "")
    result["word_count"] = len(content_str.split())
    result["char_count"] = len(content_str)

    return AgentResult(
        agent="repurpose", platform=task.platform, output=result, success=True
    )