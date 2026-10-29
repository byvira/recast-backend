"""The daily AI token rollup per workspace, written by app.shared.llm._record_workspace_usage() for every model call made inside a
usage_workspace() scope. Tokens are what the AI providers bill; what members spend is credits (app.billing.credits), and the
owner's monthly credit limit is kept in `workspace_ai_budgets.monthly_credit_limit`.
"""

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel


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
