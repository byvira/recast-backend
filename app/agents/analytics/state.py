"""
AnalyticsAgentState — single source of truth for the analytics agent graph.

Lifecycle:
  1. check_platforms_node  → fills connected_platforms
  2. fetch_metrics_node    → fills account_metrics, post_metrics
  3. analyze_node          → fills analysis
  4. recommend_node        → fills recommendations
  5. format_report_node    → fills report
"""

from typing import Any, Optional
from typing_extensions import TypedDict


class AnalyticsAgentState(TypedDict):

    # ── Identity ──────────────────────────────────────────────────────────
    user_id:  str
    question: str   # natural language question e.g. "how am I performing this week?"

    # ── Platform data — filled by check_platforms_node ───────────────────
    connected_platforms: list[str]

    # ── Metrics — filled by fetch_metrics_node ───────────────────────────
    account_metrics: list[dict]   # AccountMetrics.model_dump() per platform
    post_metrics:    list[dict]   # PostMetrics.model_dump() per post

    # ── LLM outputs ──────────────────────────────────────────────────────
    analysis:        str          # trend interpretation from analyze_node
    recommendations: list[str]    # actionable suggestions from recommend_node
    report:          str          # final natural language report

    # ── Errors ───────────────────────────────────────────────────────────
    errors: list[str]


def build_initial_state(
    user_id: str,
    question: str = "Give me a full performance overview.",
) -> AnalyticsAgentState:
    return AnalyticsAgentState(
        user_id=user_id,
        question=question,
        connected_platforms=[],
        account_metrics=[],
        post_metrics=[],
        analysis="",
        recommendations=[],
        report="",
        errors=[],
    )