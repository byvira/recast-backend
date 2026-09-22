"""Workspace cohorts — named groupings of members (e.g. "Engineering",
"Marketing"), the data layer Odette's Brand Consistency Radar needs before it
can compare real cohorts instead of showing an illustrative example.
Scaffolding: cohorts can be created and members assigned, but no brand-token
adherence scoring exists yet — that analysis (what the radar's per-axis
percentages would actually measure) is separate, later work.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class WorkspaceCohort(BaseModel):
    id: str
    workspace_id: str
    name: str
    member_user_ids: list[str] = Field(default_factory=list)
    created_by: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class WorkspaceCohortWrite(BaseModel):
    name: str
    member_user_ids: list[str] = Field(default_factory=list)
