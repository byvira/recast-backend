"""
SEO enrichment for long-form content.
Maps to extras.seoMeta toggle in ConfigPanel ExtrasToggles.
Only runs when seoMeta is True AND platform is Blog, Newsletter, or YouTube.
"""

import logging
import re
from slugify import slugify
from app.models.text import AgentTask, AgentResult, Platform
from app.shared.llm import call_llm_structured

logger = logging.getLogger(__name__)

SEO_PLATFORMS = {Platform.BLOG, Platform.NEWSLETTER, Platform.YOUTUBE}


def should_run_seo(platform: Platform, seo_meta: bool) -> bool:
    return seo_meta and platform in SEO_PLATFORMS


async def run_seo_agent(task: AgentTask, content: str) -> AgentResult:
    """
    Generate a full SEO package for long-form content.
    Returns title, meta description, primary keyword, secondary keywords,
    hashtags, URL slug, and video tags.
    """
    platform_instruction = {
        Platform.BLOG: "Focus on blog search intent. Keywords should match what a reader searches to find this article.",
        Platform.NEWSLETTER: "Focus on newsletter discoverability and email subject line SEO.",
        Platform.YOUTUBE: "Focus on YouTube search. Generate 8-12 video tags mixing broad and specific terms.",
    }.get(task.platform, "")

    prompt = f"""
You are an SEO specialist. Analyse the content and generate a complete SEO package.

{platform_instruction}

CONTENT:
{content[:2000]}

Return valid JSON only:
{{
  "title": "SEO title with primary keyword, 60 chars max",
  "meta_description": "Compelling meta description with keyword, 155 chars max",
  "primary_keyword": "the single most important search keyword",
  "secondary_keywords": ["keyword 2", "keyword 3", "keyword 4", "keyword 5"],
  "hashtags": ["specific-hashtag", "niche-hashtag"],
  "slug": "url-friendly-slug-here",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"]
}}

Rules:
- Meta description MUST be under 160 characters
- Hashtags must be specific, never generic like #content or #marketing
- Slug lowercase with hyphens only, no special characters
"""

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
