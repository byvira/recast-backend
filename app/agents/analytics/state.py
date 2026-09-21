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

# The "no real question" default — used by GET /ask (full dashboard report,
# no question needed) and AnalyticsAskRequest's default. analyze_node and
# format_report_node compare against this exact string to decide whether to
# run the fixed 4-point overview structure or genuinely answer a specific
# question — see their docstrings. Centralized here so the API layer and
# the graph nodes can never drift out of sync on what "no question" means.
DEFAULT_OVERVIEW_QUESTION = "Give me a full performance overview for the last 7 days."


class AnalyticsAgentState(TypedDict):

    # ── Identity ──────────────────────────────────────────────────────────
    workspace_id: str
    user_id:  str   # caller (audit)
    question: str   # natural language question e.g. "how am I performing this week?"
    language: str   # the calling user's language — resolved in graph.py::run_analytics

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
    workspace_id: str,
    question: str = DEFAULT_OVERVIEW_QUESTION,
    user_id: str = "",
    language: str = "en",
) -> AnalyticsAgentState:
    return AnalyticsAgentState(
        workspace_id=workspace_id,
        user_id=user_id,
        question=question,
        language=language,
        connected_platforms=[],
        account_metrics=[],
        post_metrics=[],
        analysis="",
        recommendations=[],
        report="",
        errors=[],
    )