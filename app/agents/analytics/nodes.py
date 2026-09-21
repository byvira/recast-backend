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

from app.agents.analytics.state import AnalyticsAgentState, DEFAULT_OVERVIEW_QUESTION
from app.pipelines.analytics.aggregator import (
    fetch_account_metrics_all,
    fetch_post_metrics_all,
)
from app.pipelines.publish.token_store import get_all_tokens
from app.pipelines.text.generator import resolve_language_name
from app.prompts.registry import load_fixture, load_localized, load_prompt
from app.shared.llm import call_llm, call_llm_structured, GroqModel
from app.shared.localized_strings import get_localized_string
from app.db.mongo import get_db

logger = logging.getLogger(__name__)

# Deterministic (no-LLM) fallback strings — analyze_node/recommend_node below.
# English source templates, translated into `language` on demand and cached
# via get_localized_string() — same pattern as signals.py's remy_message()
# and personas.py's odette_flag_summary(). `language` is a fully opaque
# string here, never validated or matched against a fixed set.
# Source-of-truth text lives in app/prompts/localized/analytics_fallbacks.yaml.
_ANALYTICS_FALLBACK_ENGLISH_TEMPLATES = load_localized("analytics_fallbacks")


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

        # Post metrics — recent published posts from MongoDB.
        # Each content_pieces document is already exactly one platform's
        # content (piece["platform"] + piece["platform_post_id"], set by
        # _update_piece_status in app/api/v1/publish.py) — there's no real
        # multi-platform bundle per piece, so this used to read
        # "platform_results", an array field nothing ever wrote (only
        # synthesized on the fly for the calendar API response, see
        # app/api/v1/analytics.py's get_calendar). That meant posts_to_fetch
        # was always empty and post_metrics never populated regardless of
        # how many pieces were actually published.
        db = get_db()
        published_posts = await db["content_pieces"].find(
            {
                "workspace_id":     state["workspace_id"],
                "publish_status":   "published",
                "platform_post_id": {"$exists": True, "$ne": None},
            },
            {"platform": 1, "platform_post_id": 1, "_id": 1},
        ).sort("created_at", -1).to_list(length=20)

        posts_to_fetch = [
            {
                "piece_id":         str(piece["_id"]),
                # Lowercase slug — matches token_store's platform key and
                # the aggregator's _FETCHERS dict, not the display-cased
                # content Platform value ("LinkedIn") piece["platform"] holds.
                "platform":         piece.get("platform", "").lower(),
                "platform_post_id": piece["platform_post_id"],
                "platform_user_id": "",
            }
            for piece in published_posts
        ]

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

    Was always running the same fixed 4-point checklist (best platform /
    trends / content type / underperformers) regardless of what the user
    actually asked — {{ question }} was interpolated into the prompt but
    the instructions never referenced it, so "What should I post next?"
    and "Give me a full overview" produced structurally identical output.
    Now branches: the default overview question (GET /ask, or POST /ask
    with no real question) keeps that fixed checklist; any other real
    question gets analytics/analyze_question.jinja, which answers it
    directly instead of running the generic checklist.
    """
    if not state["account_metrics"]:
        return {"analysis": await _analytics_fallback(state.get("language", "en"), "no_metrics")}

    is_overview = state["question"] == DEFAULT_OVERVIEW_QUESTION

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
    prompt = load_prompt(
        "analytics/analyze" if is_overview else "analytics/analyze_question",
        question=state["question"],
        account_summary=account_summary,
        post_summary=post_summary,
        language_name=resolve_language_name(language),
    )

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
    prompt = load_prompt(
        "analytics/recommend",
        question=state["question"],
        analysis=state["analysis"],
        language_name=resolve_language_name(language),
        example_line=load_fixture("analytics_recommend_example")["example_line"],
    )

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
# Source-of-truth English text lives in app/prompts/localized/analytics_labels.yaml.
_REPORT_LABEL_ENGLISH_TEMPLATES = load_localized("analytics_labels")


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

    Previously always built the full OVERVIEW/BEST POST/ANALYSIS/
    RECOMMENDATIONS skeleton regardless of what was asked — every question
    typed into the "Ask" chat came back looking identical (same metrics
    block, same generic structure), since only state["question"] itself
    was swapped into a header line. Now: the default overview question
    (GET /ask, or POST /ask with no real question — see analyze_node) keeps
    that full skeleton, matching the Performance page's "Report" tab, which
    expects it. Any other real question gets a short, direct Q&A format
    instead — analyze_node already made analysis itself answer the
    question directly rather than running the generic checklist; this just
    stops re-burying that direct answer under an unrelated metrics dump.
    """
    language = state.get("language", "en")
    L = await _report_labels(language)
    is_overview = state["question"] == DEFAULT_OVERVIEW_QUESTION

    recs_str = "\n".join(
        f"  {i+1}. {r}" for i, r in enumerate(state["recommendations"])
    ) if state["recommendations"] else f"  {L['no_recommendations']}"

    if not is_overview:
        report = f"""{L["question"]}: {state["question"]}

{L["answer"]}
{state["analysis"]}

{L["recommendations"]}
{recs_str}
"""
        if state["errors"]:
            report += f"\n{L['warnings']}\n" + "\n".join(f"  - {e}" for e in state["errors"])
        return {"report": report}

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