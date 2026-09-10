"""Schemas for the Layer-2 workspace-supervisor collections.

* ``workspace_insights``    — Odette's proactive, workspace-level recommendations
* ``workspace_flags``       — hard-limit (rule) + anomaly (LLM) flags
* ``admin_notifications``   — in-app admin feed; the near-real-time delivery target
* ``agent_worker_state``    — per-workspace checkpoint + debounce bookkeeping

All are workspace-scoped; every read in the routes/worker filters by
``workspace_id`` sourced from the authenticated context or the stream partition.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class FlagType(str, Enum):
    TIER_SEAT_EXCEEDED = "tier_seat_exceeded"
    DAILY_PUBLISH_CAP = "daily_publish_cap"
    RBAC_VIOLATION = "rbac_violation"
    BRAND_VOICE_INSTABILITY = "brand_voice_instability"
    MEMBER_CHURN = "member_churn"
    ASSISTANT_SIGNAL_STORM = "assistant_signal_storm"
    LLM_ANOMALY = "llm_anomaly"


class Detection(str, Enum):
    RULE = "rule"
    LLM = "llm"


class FlagSeverity(str, Enum):
    WARNING = "warning"
    CRITICAL = "critical"


class FlagStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    MUTED = "muted"


class InsightKind(str, Enum):
    RECOMMENDATION = "recommendation"
    OBSERVATION = "observation"


class InsightPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class InsightStatus(str, Enum):
    NEW = "new"
    SEEN = "seen"
    DISMISSED = "dismissed"
    ACTIONED = "actioned"


# ─────────────────────────────────────────────────────────────────────────────
# Documents
# ─────────────────────────────────────────────────────────────────────────────

class InsightEvidence(BaseModel):
    event_ids: list[str] = Field(default_factory=list)
    signal_ids: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)


class WorkspaceInsight(BaseModel):
    id: str = Field(alias="_id")
    workspace_id: str
    kind: InsightKind = InsightKind.RECOMMENDATION
    title: str
    body_persona: str                      # Odette's voice
    rationale: str = ""                     # short summary of the reasoning trace
    evidence: InsightEvidence = Field(default_factory=InsightEvidence)
    priority: InsightPriority = InsightPriority.MEDIUM
    pipeline_scope: Union[list[str], str] = "all"   # which pipelines it concerns
    langsmith_run_url: str = ""
    status: InsightStatus = InsightStatus.NEW
    created_at: datetime
    updated_at: datetime
    created_by_agent_run: str = ""

    model_config = {"populate_by_name": True}


class FlagMetric(BaseModel):
    name: str = ""
    value: float = 0.0
    limit: float = 0.0


class FlagNotified(BaseModel):
    in_app: bool = False
    email: bool = False
    at: Optional[datetime] = None


class WorkspaceFlag(BaseModel):
    id: str = Field(alias="_id")
    workspace_id: str
    flag_type: FlagType
    detection: Detection
    severity: FlagSeverity = FlagSeverity.WARNING
    summary_persona: str                    # Odette's voice
    detail: dict = Field(default_factory=dict)
    metric: FlagMetric = Field(default_factory=FlagMetric)
    langsmith_run_url: Optional[str] = None      # set only when detection == LLM
    status: FlagStatus = FlagStatus.OPEN
    notified: FlagNotified = Field(default_factory=FlagNotified)
    created_at: datetime
    resolved_at: Optional[datetime] = None

    model_config = {"populate_by_name": True}


class NotificationSource(BaseModel):
    kind: str          # "flag" | "insight"
    id: str


class AdminNotification(BaseModel):
    id: str = Field(alias="_id")
    workspace_id: str
    audience: str = "admins"               # resolved to owner+admin user_ids at read time
    title: str
    body_persona: str                      # Odette's voice
    source: NotificationSource
    severity: str = "warning"
    read_by: list[str] = Field(default_factory=list)   # per-admin read state
    created_at: datetime

    model_config = {"populate_by_name": True}


class AgentWorkerState(BaseModel):
    """``_id`` is ``"supervisor:{workspace_id}"`` or ``"personal:global"``."""

    id: str = Field(alias="_id")
    last_event_id_processed: str = "0"     # Redis stream ID ("0" = from the start)
    last_llm_pass_at: Optional[datetime] = None
    events_since_last_llm: int = 0
    lock_until: Optional[datetime] = None  # coalescing lock for the reasoning pass
    updated_at: Optional[datetime] = None

    model_config = {"populate_by_name": True}
