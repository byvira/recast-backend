"""
Analytics agent graph nodes.
Each node receives AnalyticsAgentState and returns only the fields it updates.

Node responsibilities:
  check_platforms_node  → find which platforms user has connected
  fetch_metrics_node    → fetch account + post metrics for all platforms
  analyze_node          → LLM interprets trends and patterns
  recommend_node        → LLM generates actionable recommendations
  format_report_node    → assemble final natural language report
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from app.agents.analytics.state import AnalyticsAgentState
from app.pipelines.analytics.aggregator import (
    fetch_account_metrics_all,
    fetch_post_metrics_all,
)
from app.pipelines.publish.token_store import get_all_tokens
from app.pipelines.text.generator import resolve_language_name
from app.shared.llm import call_llm, call_llm_structured, GroqModel
from app.shared.localized_strings import get_localized_string
from app.db.mongo import get_db

logger = logging.getLogger(__name__)

# Deterministic (no-LLM) fallback strings — analyze_node/recommend_node below.
# English source templates, translated into `language` on demand and cached
# via get_localized_string() — same pattern as signals.py's remy_message()
# and personas.py's odette_flag_summary(). `language` is a fully opaque
# string here, never validated or matched against a fixed set.
_ANALYTICS_FALLBACK_ENGLISH_TEMPLATES = {
    "no_metrics": "No metrics available to analyze.",
    "analysis_unavailable": "Analysis unavailable.",
    "connect_accounts": "Connect social accounts to get recommendations.",
}


async def _analytics_fallback(language: str, key: str) -> str:
    template = _ANALYTICS_FALLBACK_ENGLISH_TEMPLATES[key]
    return await get_localized_string(f"analytics.fallback.{key}", language, template)


# ─────────────────────────────────────────────────────────────────────────────
# NODES
# ─────────────────────────────────────────────────────────────────────────────

async def check_platforms_node(state: AnalyticsAgentState) -> dict:
    """
    Find all connected platforms for the user.
    Writes: connected_platforms
    """
    try:
        accounts = await get_all_tokens(state["workspace_id"])
        platforms = [a["platform"] for a in accounts if a.get("is_active")]
        logger.info(
            "Analytics agent — workspace %s connected platforms: %s",
            state["workspace_id"], platforms,
        )
        return {"connected_platforms": platforms}

    except Exception as exc:
        logger.error("check_platforms_node failed: %s", exc)
        return {
            "connected_platforms": [],
            "errors": state["errors"] + [f"Failed to load platforms: {exc}"],
        }


async def fetch_metrics_node(state: AnalyticsAgentState) -> dict:
    """
    Fetch account-level and post-level metrics for all connected platforms.
    Writes: account_metrics, post_metrics
    """
    if not state["connected_platforms"]:
        return {
            "account_metrics": [],
            "post_metrics":    [],
            "errors": state["errors"] + ["No connected platforms found."],
        }

    try:
        since = datetime.now(timezone.utc) - timedelta(days=7)
        until = datetime.now(timezone.utc)

        # Account metrics — one per platform
        account_metrics = await fetch_account_metrics_all(
            workspace_id=state["workspace_id"],
            platforms=state["connected_platforms"],
            since=since,
            until=until,
        )

        # Post metrics — recent published posts from MongoDB
        db = get_db()
        published_posts = await db["content_pieces"].find(
            {
                "workspace_id":   state["workspace_id"],
                "publish_status": "published",
            },
            {"platform_results": 1, "_id": 1},
        ).sort("created_at", -1).to_list(length=20)

        posts_to_fetch = []
        for piece in published_posts:
            for result in piece.get("platform_results", []):
                if result.get("platform_post_id"):
                    posts_to_fetch.append({
                        "piece_id":         str(piece["_id"]),
                        "platform":         result["platform"],
                        "platform_post_id": result["platform_post_id"],
                        "platform_user_id": result.get("platform_user_id", ""),
                    })

        post_metrics = await fetch_post_metrics_all(
            workspace_id=state["workspace_id"],
            posts=posts_to_fetch,
        ) if posts_to_fetch else []

        logger.info(
            "Metrics fetched — %d account, %d posts",
            len(account_metrics), len(post_metrics),
        )

        return {
            "account_metrics": [m.model_dump() for m in account_metrics],
            "post_metrics":    [m.model_dump() for m in post_metrics],
        }

    except Exception as exc:
        logger.error("fetch_metrics_node failed: %s", exc)
        return {
            "account_metrics": [],
            "post_metrics":    [],
            "errors": state["errors"] + [f"Failed to fetch metrics: {exc}"],
        }


async def analyze_node(state: AnalyticsAgentState) -> dict:
    """
    LLM interprets the metrics — finds trends, patterns, anomalies.
    Writes: analysis
    """
    if not state["account_metrics"]:
        return {"analysis": await _analytics_fallback(state.get("language", "en"), "no_metrics")}

    # Build a clean metrics summary for the LLM
    account_summary = "\n".join([
        f"- {m['platform'].upper()}: {m['followers']} followers, "
        f"{m['total_impressions']} impressions, {m['total_reach']} reach"
        for m in state["account_metrics"]
    ])

    post_summary = ""
    if state["post_metrics"]:
        top_posts = sorted(
            state["post_metrics"],
            key=lambda p: p.get("engagement_rate", 0),
            reverse=True,
        )[:5]
        post_summary = "\n".join([
            f"- {p['platform'].upper()} post: "
            f"{p['likes']} likes, {p['comments']} comments, "
            f"{p.get('reposts', 0)} reposts, "
            f"engagement rate {p.get('engagement_rate', 0):.2f}%"
            for p in top_posts
        ])

    language = state.get("language", "en")
    # No `if language == "en": skip` branch — found via audit that this
    # contradicted generator.py's own build_language_instruction(), which
    # explicitly documents NOT special-casing English so every language
    # (including "en") goes through the identical code path. Wording is also
    # written to hold for English itself (no "not English" caveat that would
    # self-contradict when the target language IS English).
    language_line = (
        f"Write the analysis in {resolve_language_name(language)} — that is the language "
        f"the person reading this dashboard reads.\n"
    )
    prompt = f"""You are a social media analytics expert.

User question: {state["question"]}

ACCOUNT METRICS (last 7 days):
{account_summary}

TOP PERFORMING POSTS:
{post_summary if post_summary else "No published posts yet."}

{language_line}Analyze this data and provide:
1. Which platform is performing best and why
2. Any notable trends or patterns
3. What content type is getting the most engagement
4. Any platforms that are underperforming

Be specific, data-driven, and concise. 2-3 sentences per point."""

    try:
        # max_tokens set explicitly — same reasoning-token-exhaustion risk as
        # generator.py's GENERATION_MAX_TOKENS for non-English requests.
        analysis = await call_llm(
            prompt=prompt,
            model=GroqModel.BALANCED,
            temperature=0.3,
            max_tokens=4000,
        )
        return {"analysis": analysis}

    except Exception as exc:
        logger.error("analyze_node failed: %s", exc)
        return {
            "analysis": await _analytics_fallback(state.get("language", "en"), "analysis_unavailable"),
            "errors": state["errors"] + [f"Analysis failed: {exc}"],
        }


async def recommend_node(state: AnalyticsAgentState) -> dict:
    """
    LLM generates specific, actionable recommendations based on the analysis.
    Writes: recommendations
    """
    # Was a string-equality check against analyze_node's old hardcoded English
    # fallback text ("No metrics available to analyze.") — broke the moment
    # that fallback became language-aware, since a Tamil/Hindi/Korean workspace
    # would never match the English literal. Check the actual underlying
    # condition (no account metrics) instead of matching translated prose.
    if not state["analysis"] or not state["account_metrics"]:
        return {"recommendations": [await _analytics_fallback(state.get("language", "en"), "connect_accounts")]}

    language = state.get("language", "en")
    # No English-skip branch — see analyze_node's identical fix above.
    language_line = f"Write each recommendation string in {resolve_language_name(language)}.\n"
    prompt = f"""Based on this analytics analysis:

{state["analysis"]}

Generate 3-5 specific, actionable recommendations for this user.
Each recommendation should be:
- Concrete (what exactly to do)
- Platform-specific where relevant
- Achievable within the next 7 days

{language_line}Respond ONLY as a JSON array of strings.
Example: ["Post 3x per week on LinkedIn", "Use more video on Instagram"]
No explanation, no markdown, just the JSON array."""

    try:
        # max_tokens raised — same reasoning-token-exhaustion risk as
        # generator.py's GENERATION_MAX_TOKENS for non-English requests.
        result = await call_llm_structured(
            prompt=prompt,
            model=GroqModel.BALANCED,
            max_tokens=4000,
        )

        # result may be a list directly or wrapped in a key
        if isinstance(result, list):
            recommendations = result
        elif isinstance(result, dict):
            recommendations = (
                result.get("recommendations")
                or result.get("items")
                or list(result.values())[0]
                if result else []
            )
        else:
            recommendations = []

        return {"recommendations": recommendations}

    except Exception as exc:
        logger.error("recommend_node failed: %s", exc)
        return {
            "recommendations": [],
            "errors": state["errors"] + [f"Recommendations failed: {exc}"],
        }


# English source labels — translated into `language` on demand and cached
# via get_localized_string(), one Mongo doc per (key, language). Replaces the
# earlier static en/ta/hi/ko dict entirely; `language` is never validated or
# matched against a fixed set. All lookups for a report happen concurrently
# via asyncio.gather since none of them depend on each other.
_REPORT_LABEL_ENGLISH_TEMPLATES = {
    "title": "📊 ANALYTICS REPORT", "question": "Question", "platforms": "Platforms",
    "period": "Period", "period_value": "Last 7 days", "overview": "📈 OVERVIEW",
    "total_followers": "Total Followers:", "total_impressions": "Total Impressions:",
    "total_reach": "Total Reach:", "posts_analyzed": "Posts Analyzed:",
    "best_post": "🏆 BEST PERFORMING POST", "no_posts": "No posts yet.",
    "engagement": "engagement rate", "likes": "likes", "comments": "comments",
    "analysis": "🔍 ANALYSIS", "recommendations": "💡 RECOMMENDATIONS",
    "no_recommendations": "No recommendations available.", "warnings": "⚠️  WARNINGS",
    "none": "None",
}


async def _report_labels(language: str) -> dict[str, str]:
    keys = list(_REPORT_LABEL_ENGLISH_TEMPLATES.keys())
    values = await asyncio.gather(*(
        get_localized_string(f"analytics.report.{k}", language, _REPORT_LABEL_ENGLISH_TEMPLATES[k])
        for k in keys
    ))
    return dict(zip(keys, values))


async def format_report_node(state: AnalyticsAgentState) -> dict:
    """
    Assembles the final natural language report from all state fields.
    Writes: report

    Section labels are translated + cached via get_localized_string() (same
    pattern as remy_message()/odette_flag_summary()) — previously hardcoded
    English regardless of state["language"], so even a fully-translated
    analysis/recommendations body was still wrapped in an English-only
    skeleton ("ANALYTICS REPORT", "OVERVIEW", ...).
    """
    language = state.get("language", "en")
    L = await _report_labels(language)

    platforms_str = ", ".join(
        p.upper() for p in state["connected_platforms"]
    ) or L["none"]

    total_followers   = sum(m.get("followers", 0)         for m in state["account_metrics"])
    total_impressions = sum(m.get("total_impressions", 0)  for m in state["account_metrics"])
    total_reach       = sum(m.get("total_reach", 0)        for m in state["account_metrics"])
    total_posts       = len(state["post_metrics"])

    best_post = max(
        state["post_metrics"],
        key=lambda p: p.get("engagement_rate", 0),
        default=None,
    )
    best_post_line = (
        f"{best_post['platform'].upper()} — {best_post.get('engagement_rate', 0):.2f}% "
        f"{L['engagement']} ({best_post.get('likes', 0)} {L['likes']}, "
        f"{best_post.get('comments', 0)} {L['comments']})"
        if best_post else L["no_posts"]
    )

    recs_str = "\n".join(
        f"  {i+1}. {r}" for i, r in enumerate(state["recommendations"])
    ) if state["recommendations"] else f"  {L['no_recommendations']}"

    report = f"""{L["title"]}
{'=' * 50}
{L["question"]}: {state["question"]}
{L["platforms"]}: {platforms_str}
{L["period"]}: {L["period_value"]}

{L["overview"]}
  {L["total_followers"]}   {total_followers:,}
  {L["total_impressions"]} {total_impressions:,}
  {L["total_reach"]}       {total_reach:,}
  {L["posts_analyzed"]}    {total_posts}

{L["best_post"]}
  {best_post_line}

{L["analysis"]}
{state["analysis"]}

{L["recommendations"]}
{recs_str}
"""

    if state["errors"]:
        report += f"\n{L['warnings']}\n" + "\n".join(f"  - {e}" for e in state["errors"])

    return {"report": report}