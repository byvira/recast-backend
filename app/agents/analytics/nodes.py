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

import logging
from datetime import datetime, timezone, timedelta

from app.agents.analytics.state import AnalyticsAgentState
from app.pipelines.analytics.aggregator import (
    fetch_account_metrics_all,
    fetch_post_metrics_all,
)
from app.pipelines.publish.token_store import get_all_tokens
from app.shared.llm import call_llm, call_llm_structured, GroqModel
from app.db.mongo import get_db

logger = logging.getLogger(__name__)


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
        return {"analysis": "No metrics available to analyze."}

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

    prompt = f"""You are a social media analytics expert.

User question: {state["question"]}

ACCOUNT METRICS (last 7 days):
{account_summary}

TOP PERFORMING POSTS:
{post_summary if post_summary else "No published posts yet."}

Analyze this data and provide:
1. Which platform is performing best and why
2. Any notable trends or patterns
3. What content type is getting the most engagement
4. Any platforms that are underperforming

Be specific, data-driven, and concise. 2-3 sentences per point."""

    try:
        analysis = await call_llm(
            prompt=prompt,
            model=GroqModel.BALANCED,
            temperature=0.3,
        )
        return {"analysis": analysis}

    except Exception as exc:
        logger.error("analyze_node failed: %s", exc)
        return {
            "analysis": "Analysis unavailable.",
            "errors": state["errors"] + [f"Analysis failed: {exc}"],
        }


async def recommend_node(state: AnalyticsAgentState) -> dict:
    """
    LLM generates specific, actionable recommendations based on the analysis.
    Writes: recommendations
    """
    if not state["analysis"] or state["analysis"] == "No metrics available to analyze.":
        return {"recommendations": ["Connect social accounts to get recommendations."]}

    prompt = f"""Based on this analytics analysis:

{state["analysis"]}

Generate 3-5 specific, actionable recommendations for this user.
Each recommendation should be:
- Concrete (what exactly to do)
- Platform-specific where relevant
- Achievable within the next 7 days

Respond ONLY as a JSON array of strings.
Example: ["Post 3x per week on LinkedIn", "Use more video on Instagram"]
No explanation, no markdown, just the JSON array."""

    try:
        result = await call_llm_structured(
            prompt=prompt,
            model=GroqModel.BALANCED,
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


async def format_report_node(state: AnalyticsAgentState) -> dict:
    """
    Assembles the final natural language report from all state fields.
    Writes: report
    """
    platforms_str = ", ".join(
        p.upper() for p in state["connected_platforms"]
    ) or "None"

    total_followers   = sum(m.get("followers", 0)         for m in state["account_metrics"])
    total_impressions = sum(m.get("total_impressions", 0)  for m in state["account_metrics"])
    total_reach       = sum(m.get("total_reach", 0)        for m in state["account_metrics"])
    total_posts       = len(state["post_metrics"])

    best_post = max(
        state["post_metrics"],
        key=lambda p: p.get("engagement_rate", 0),
        default=None,
    )

    recs_str = "\n".join(
        f"  {i+1}. {r}" for i, r in enumerate(state["recommendations"])
    ) if state["recommendations"] else "  No recommendations available."

    report = f"""📊 ANALYTICS REPORT
{'=' * 50}
Question: {state["question"]}
Platforms: {platforms_str}
Period: Last 7 days

📈 OVERVIEW
  Total Followers:   {total_followers:,}
  Total Impressions: {total_impressions:,}
  Total Reach:       {total_reach:,}
  Posts Analyzed:    {total_posts}

🏆 BEST PERFORMING POST
  {f"{best_post['platform'].upper()} — {best_post.get('engagement_rate', 0):.2f}% engagement rate ({best_post.get('likes', 0)} likes, {best_post.get('comments', 0)} comments)" if best_post else "No posts yet."}

🔍 ANALYSIS
{state["analysis"]}

💡 RECOMMENDATIONS
{recs_str}
"""

    if state["errors"]:
        report += f"\n⚠️  WARNINGS\n" + "\n".join(f"  - {e}" for e in state["errors"])

    return {"report": report}