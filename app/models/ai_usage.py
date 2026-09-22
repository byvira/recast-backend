"""Per-workspace AI processing budget + daily usage rollup — the data layer
Odette's Quotas tab needs before "AI Processing Budget" can be a real number
instead of an admitted gap. Scaffolding: the budget is settable and a daily
usage collection exists, but nothing increments it yet — no LLM call site
(Groq, embeddings, etc.) writes here. Instrumenting every call site is
separate, later work; wiring it in one place (app.shared.llm) once decided
is preferable to bolting it onto each pipeline individually.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class WorkspaceAIBudget(BaseModel):
    id: str                       # == workspace_id
    workspace_id: str
    monthly_token_budget: Optional[int] = None   # None = unmetered / no cap set
    updated_at: Optional[datetime] = None


class WorkspaceAIBudgetWrite(BaseModel):
    monthly_token_budget: Optional[int] = None


class AIUsageDailyRecord(BaseModel):
    """One row per workspace per UTC day. Not written by anything yet —
    present so GET /ops/ai-usage has a real (currently always-empty)
    collection to read from rather than a fabricated number."""
    id: str                       # f"{workspace_id}:{date}"
    workspace_id: str
    date: str                     # YYYY-MM-DD
    tokens_used: int = 0
    calls: int = 0
