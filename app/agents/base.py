from typing import TypedDict, Any
from datetime import datetime

class BaseAgentState(TypedDict):
    user_id: str
    brand: dict
    raw_input: Any
    plan: list[str]
    current_step: str
    tool_calls: list[dict]
    retry_count: int
    max_retries: int
    intermediate_outputs: dict
    quality_scores: dict
    outputs: dict
    errors: list[str]
    status: str
    started_at: str
    completed_at: str

def initial_state(
    user_id: str,
    brand: dict,
    raw_input: Any,
    max_retries: int = 2,
) -> BaseAgentState:
    """Build a clean initial state for any agent."""
    return BaseAgentState(
        user_id=user_id,
        brand=brand,
        raw_input=raw_input,
        plan=[],
        current_step="",
        tool_calls=[],
        retry_count=0,
        max_retries=max_retries,
        intermediate_outputs={},
        quality_scores={},
        outputs={},
        errors=[],
        status="thinking",
        started_at=datetime.utcnow().isoformat(),
        completed_at="",
    )

def avg_quality(state: BaseAgentState) -> float:
    """Calculate average quality score across all outputs."""
    scores = state["quality_scores"]
    if not scores:
        return 0.0
    return sum(scores.values()) / len(scores)

def should_retry(state: BaseAgentState) -> bool:
    """Decide if agent should retry based on quality and retry count."""
    return (
        avg_quality(state) < 0.75
        and state["retry_count"] < state["max_retries"]
    )


_DIMENSION_WORDS = {
    "completeness": "completeness, covering everything that matters in the source",
    "brand_alignment": "brand voice, how closely it sounds like the brand",
    "accuracy": "accuracy, staying true to what was actually said",
    "platform_fit": "platform fit, the shape and length that suit the platform",
    "engagement_potential": "engagement, the opening and the reasons to keep reading",
}


def weakest_dimension(scores: dict) -> str:
    """Plain words for the lowest scoring part of a draft, so a retry can be told what to fix first. Empty when unknown."""
    graded = {k: v for k, v in (scores or {}).items() if k in _DIMENSION_WORDS and isinstance(v, (int, float))}
    if not graded:
        return ""
    return _DIMENSION_WORDS[min(graded, key=graded.get)]
