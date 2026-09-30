"""Per-workspace AI processing budget + daily usage rollup — the data layer
Odette's Quotas tab needs before "AI Processing Budget" can be a real number
instead of an admitted gap. The budget is settable; the daily usage
collection is now written by app.shared.llm._record_workspace_usage() for
call sites wrapped in usage_workspace() (Odette, Remy) — see
AIUsageDailyRecord's docstring for which call sites still aren't.
"""

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel


class WorkspaceAIBudget(BaseModel):
    id: str                       # == workspace_id
    workspace_id: str
    monthly_token_budget: Optional[int] = None   # None = unmetered / no cap set
    updated_at: Optional[datetime] = None


class WorkspaceAIBudgetWrite(BaseModel):
    monthly_token_budget: Optional[int] = None


class AIUsageDailyRecord(BaseModel):
    """One row per workspace per UTC day. Now written by
    app.shared.llm._record_workspace_usage() whenever a call happens inside
    a usage_workspace(workspace_id) scope (currently: Odette's reasoning +
    synthesis calls, Remy's align_draft) — see that function's docstring.
    Pipelines not yet wrapped in usage_workspace() are still recorded by
    app.shared.llm_health, just not counted here."""
    id: str                       # f"{workspace_id}:{date}"
    workspace_id: str
    date: str                     # YYYY-MM-DD
    tokens_used: int = 0
    calls: int = 0
