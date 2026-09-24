"""Per-workspace AI processing budget + daily usage rollup — the data layer
Odette's Quotas tab needs before "AI Processing Budget" can be a real number
instead of an admitted gap. The budget is settable; the daily usage
collection is now written by app.shared.llm._record_workspace_usage() for
call sites wrapped in usage_workspace() (Odette, Remy) — see
AIUsageDailyRecord's docstring for which call sites still aren't.

Also holds OpsLLMNote — the Ops LLM Health page's manually-logged
issue/security audit trail.
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
    Pipelines not yet wrapped in usage_workspace() still count toward the
    process-lifetime totals in get_usage_stats(), just not here."""
    id: str                       # f"{workspace_id}:{date}"
    workspace_id: str
    date: str                     # YYYY-MM-DD
    tokens_used: int = 0
    calls: int = 0


class OpsLLMNoteKind(str, Enum):
    ISSUE = "issue"
    SECURITY = "security"


class OpsLLMNoteStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class OpsLLMNote(BaseModel):
    """A manually-logged item on the Ops LLM Health page — real records the
    team adds, not synthetic/derived data. Deliberately separate from
    workspace_flags (customer-facing, per-tenant anomaly detection) — this
    is the team's own internal audit trail across the whole platform."""
    id: str
    kind: OpsLLMNoteKind
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    title: str
    detail: str = ""
    status: OpsLLMNoteStatus = OpsLLMNoteStatus.OPEN
    created_by: str
    created_at: datetime
    resolved_at: Optional[datetime] = None


class OpsLLMNoteWrite(BaseModel):
    kind: OpsLLMNoteKind
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    title: str
    detail: str = ""


class OpsLLMNoteStatusWrite(BaseModel):
    status: OpsLLMNoteStatus
