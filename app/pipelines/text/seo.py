"""
SEO enrichment for long-form content.
Maps to extras.seoMeta toggle in ConfigPanel ExtrasToggles.
Always runs for Blog and Newsletter; for YouTube only when seoMeta is True.
"""

import logging
import re
from slugify import slugify
from app.models.text import AgentTask, AgentResult, Platform
from app.pipelines.text.generator import build_language_instruction
from app.prompts.registry import load_prompt
from app.shared.llm import call_llm_structured

logger = logging.getLogger(__name__)

SEO_PLATFORMS = {Platform.BLOG, Platform.NEWSLETTER, Platform.YOUTUBE}


#: A Blog post always has a title, summary and tags, and a Newsletter always has a subject and preview line: the editors for them
#: read these, so they are made with the post whether or not the optional SEO extra is switched on. YouTube keeps the option.
ALWAYS_PLATFORMS = {Platform.BLOG, Platform.NEWSLETTER}


def should_run_seo(platform: Platform, seo_meta: bool) -> bool:
    if platform in ALWAYS_PLATFORMS:
        return True
    return seo_meta and platform in SEO_PLATFORMS


async def run_seo_agent(task: AgentTask, content: str) -> AgentResult:
    """
    Generate a full SEO package for long-form content.
    Returns title, meta description, primary keyword, secondary keywords,
    hashtags, URL slug, and video tags.
    """
    platform_instruction = load_prompt("text/seo/platform_focus", platform=task.platform.name)

    prompt = load_prompt(
        "text/seo/master", platform_instruction=platform_instruction, content=content[:2000],
        language_line=build_language_instruction(task.metadata.get("language", "en")),
    )

    result = await call_llm_structured(prompt)

    if not result:
        return AgentResult(agent="seo", platform=task.platform, output={}, success=False)

    meta = result.get("meta_description", "")
    if len(meta) > 160:
        result["meta_description"] = meta[:157] + "..."

    slug = result.get("slug", "")
    if not slug or not re.match(r"^[a-z0-9-]+$", slug):
        result["slug"] = slugify(result.get("title", "content"), separator="-", lowercase=True)

    return AgentResult(agent="seo", platform=task.platform, output=result, success=True)
