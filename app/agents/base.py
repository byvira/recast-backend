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
