"""
Normalise frontend input modes to NormalisedInput.
write mode   → source_type TEXT       → pass through
prompt mode  → source_type TOPIC      → research via LLM
url mode     → source_type URL        → scrape then text
repurpose    → source_type TEXT       → pass through (repurpose agent handles it)
transcript   → source_type TRANSCRIPT → pass through (from audio/video pipeline)
"""

import logging
from uuid import uuid4

from app.models.text import ContentIntent, InputSourceType, NormalisedInput, Platform
from app.pipelines.text.scraper import scrape_url
from app.shared.llm import GroqModel, call_llm, call_llm_structured

logger = logging.getLogger(__name__)

INTENT_PLATFORM_MAP = {
    Platform.BLOG: ContentIntent.BLOG,
    Platform.NEWSLETTER: ContentIntent.NEWSLETTER,
    Platform.TWITTER_THREAD: ContentIntent.THREAD,
    Platform.INSTAGRAM: ContentIntent.CAPTION,
    Platform.YOUTUBE: ContentIntent.DESCRIPTION,
    Platform.LINKEDIN: ContentIntent.POST,
    Platform.TWITTER: ContentIntent.POST,
    Platform.FACEBOOK: ContentIntent.POST,
}


def detect_intent(platforms: list[Platform]) -> ContentIntent:
    if len(platforms) == 1:
        return INTENT_PLATFORM_MAP.get(platforms[0], ContentIntent.POST)
    return ContentIntent.POST


async def research_topic(topic: str, language: str = "en") -> str:
    """
    Expand a bare keyword or topic into a 200-300 word research brief.
    Used when frontend is in prompt mode.
    """
    prompt = f"""
Generate a detailed content brief for the topic: "{topic}"
Language: {language}

Include:
- The core idea and angle
- 3-5 key points or insights
- Relevant statistics or data if applicable
- The audience pain points this topic addresses
- Actionable takeaways

Output a comprehensive 200-300 word brief that a content writer can use immediately as source material.
Write in plain paragraphs. No headers.
"""
    return await call_llm(prompt, model=GroqModel.BALANCED)


async def normalise_input(
    source_type: InputSourceType,
    content: str,
    platforms: list[Platform],
    user_id: str,
    brand_id: str,
    workspace_id: str = "",
    language: str = "en",
    intent: ContentIntent = ContentIntent.AUTO,
) -> NormalisedInput:
    """
    Entry point for normalisation. Call this before any agent runs.
    All four frontend input modes converge here to a plain string.
    """
    session_id = str(uuid4())
    raw_content = content

    if source_type == InputSourceType.URL:
        logger.info("Scraping URL for session %s", session_id)
        raw_content = await scrape_url(content)
        if len(raw_content) < 100:
            raise ValueError(f"Could not extract readable content from URL: {content}")

    elif source_type == InputSourceType.TOPIC:
        logger.info("Researching topic for session %s", session_id)
        raw_content = await research_topic(content, language)

    # Hard cap at 8000 chars — enough context for any platform without burning tokens
    if len(raw_content) > 8000:
        raw_content = raw_content[:8000]
        logger.warning("Input truncated to 8000 chars — session %s", session_id)

    detected_intent = (
        detect_intent(platforms)
        if intent is None or intent == ContentIntent.AUTO
        else intent
    )

    return NormalisedInput(
        source_type=source_type,
        raw_content=raw_content,
        detected_intent=detected_intent,
        workspace_id=workspace_id,
        user_id=user_id,
        brand_id=brand_id,
        target_platforms=platforms,
        session_id=session_id,
        language=language,
    )


async def clean_raw_content(content: str) -> str:
    """
    Light cleanup pass on any normalised input before it reaches agents.
    Runs inside normalise_node in the graph — after heavy normalisation
    (scraping, topic research) is already done by normalise_input().

    What it does:
      - Fixes obvious typos and grammar errors
      - Removes duplicate lines or repeated paragraphs
      - Removes boilerplate noise — cookie notices, newsletter signup prompts,
        read-more links, navigation text — if any slipped through scraping
      - Preserves ALL original ideas, facts, numbers, and opinions exactly
      - Does not rewrite or rephrase — only cleans

    If LLM call fails for any reason, returns original content unchanged.
    This node must never block the pipeline.
    """
    if not content or len(content.strip()) < 50:
        return content

    prompt = f"""
Clean up the following content for use as source material for content generation.

Rules:
- Fix obvious typos and spelling errors
- Remove duplicate lines or repeated paragraphs
- Remove boilerplate noise: cookie notices, newsletter signup prompts,
  navigation text, "read more" links, advertisement copy
- Preserve ALL original ideas, facts, numbers, opinions, and examples exactly
- Do not rewrite, rephrase, or improve sentences — only clean
- If the content is already clean, return it unchanged

CONTENT:
{content[:6000]}

Return valid JSON only:
{{"cleaned": "the cleaned content here"}}
"""
    try:
        result = await call_llm_structured(prompt,model=GroqModel.FAST)
        if result and result.get("cleaned"):
            cleaned = result["cleaned"].strip()
            # Sanity check — cleaned content should not be drastically shorter
            # If LLM over-stripped, return original
            if len(cleaned) > len(content) * 0.4:
                return cleaned
            logger.warning(
                "clean_raw_content: cleaned output too short (%d vs %d) — returning original",
                len(cleaned), len(content),
            )
            return content
    except Exception as e:
        logger.warning("clean_raw_content failed — returning original. Error: %s", e)

    return content


async def extract_content_brief(content: str, language: str = "en") -> str:
    """
    Pre-analysis step — runs after clean_raw_content, before generation.
    Extracts the sharpest angle, most concrete detail, and what to avoid
    from the source content. The brief is injected into every generation
    prompt between brand context and platform rules.

    Purpose: forces the generator to lead with the most specific,
    interesting angle rather than defaulting to a generic take.

    ``language`` matters more than it looks: the returned brief is spliced
    directly into the main generation prompt as "Lead with this angle:
    {sharpest_angle}". Confirmed via live testing (2026-09-11, Hindi) that
    when this function answered in English regardless of the requested
    generation language, the generator would then literally open the piece
    with that English sentence before switching to the target language for
    the rest — i.e. an English brief silently overrides a correct language
    instruction elsewhere in the pipeline. The brief's own language must
    therefore match the generation language, not just the final prompt text.

    If LLM call fails, returns empty string — generation continues without brief.
    An empty brief is safe — generation still works, just without pre-analysis sharpening.
    """
    if not content or len(content.strip()) < 50:
        return ""

    from app.pipelines.text.generator import resolve_language_name
    language_name = resolve_language_name(language)

    prompt = f"""
Analyse this content and identify the single most specific, interesting,
and non-obvious angle in it.

Rules:
- Ignore generic observations that could apply to any content on this topic
- Find the concrete detail: a specific number, a named outcome, a real moment,
  a counterintuitive point, or a surprising insight
- Identify what the lazy generic take would be so the writer can avoid it
- Write every field's value in {language_name}. This brief is quoted directly
  into a generation prompt as "Lead with this angle: <sharpest_angle>" — if
  you answer in English here, the final content will start in English too.

CONTENT:
{content[:2000]}

Return valid JSON only, with every string value written in {language_name}:
{{
  "sharpest_angle": "the single most specific and interesting angle to lead with",
  "concrete_detail": "the most specific fact, number, moment, or example in the content",
  "avoid": "the obvious generic take that anyone would write about this topic"
}}
"""
    try:
        # max_tokens raised for the same reason as generator.py's
        # GENERATION_MAX_TOKENS — gpt-oss models spend hidden reasoning
        # tokens out of the same budget as the visible JSON output, and
        # non-English requests were observed to exhaust the 2500 default.
        result = await call_llm_structured(prompt, model=GroqModel.FAST, max_tokens=3000)
        if not result:
            return ""

        sharpest = result.get("sharpest_angle", "").strip()
        concrete = result.get("concrete_detail", "").strip()
        avoid = result.get("avoid", "").strip()

        if not sharpest and not concrete:
            return ""

        lines = ["CONTENT BRIEF — use this to sharpen the writing:"]
        if sharpest:
            lines.append(f"Lead with this angle: {sharpest}")
        if concrete:
            lines.append(f"Use this specific detail: {concrete}")
        if avoid:
            lines.append(f"Do NOT write the generic take: {avoid}")
        lines.append("")

        return "\n".join(lines)

    except Exception as e:
        logger.warning("extract_content_brief failed — continuing without brief. Error: %s", e)
        return ""