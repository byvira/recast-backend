"""Tool definitions for reading: who the caller is, credits and prices, plan limits, posts and campaigns.

Reads change nothing and cost nothing. Each has a typed input (including a page size and a cursor where it lists things) and a typed
output, so an API, a command line and an assistant all return the same facts.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.tools.contract import Mode, Risk, Scope, ToolSpec


class NoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WhoAmI(BaseModel):
    workspace_id: str
    workspace_name: str
    role: Literal["owner", "admin", "editor", "viewer"]
    plan: str


class CreditsSummary(BaseModel):
    period: str
    resets_on: str
    plan_name: str
    enforced: bool
    allowance: Optional[int] = None
    used: int
    reserved: int
    left: Optional[int] = None
    percent: Optional[int] = None
    state: Optional[Literal["ok", "near", "over"]] = None
    owner_limit: Optional[int] = None


class PriceRow(BaseModel):
    key: str
    label: str
    module: str
    credits: int
    per: Optional[str] = None
    per_size: Optional[int] = None
    kind: Literal["active", "passive"]


class Prices(BaseModel):
    prices: list[PriceRow]


class LimitRow(BaseModel):
    key: str
    label: str
    unit: str
    limit: Optional[int] = None
    used: Optional[float] = None


class PlanLimits(BaseModel):
    plan: str
    enforced: bool
    limits: list[LimitRow]


class PageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(20, ge=1, le=50)
    cursor: Optional[str] = Field(None, max_length=200)


class ListPostsIn(PageIn):
    platform: Optional[str] = Field(None, max_length=40)
    stage: Optional[Literal["drafting", "staging", "scheduled", "published", "archived"]] = None
    q: Optional[str] = Field(None, max_length=200)


class PostRow(BaseModel):
    id: str
    platform: str
    status: str
    excerpt: str = Field(max_length=300)
    created_at: str


class PostPage(BaseModel):
    items: list[PostRow]
    next_cursor: Optional[str] = None


class ListCampaignsIn(PageIn):
    status: Optional[Literal["draft", "active", "paused", "completed"]] = None


class CampaignRow(BaseModel):
    id: str
    name: str
    status: str
    platforms: list[str]
    created_at: str


class CampaignPage(BaseModel):
    items: list[CampaignRow]
    next_cursor: Optional[str] = None


class WaitingIn(PageIn):
    platform: Optional[str] = Field(None, max_length=40)


class CalendarIn(PageIn):
    start: Optional[date] = None
    end: Optional[date] = None

    @model_validator(mode="after")
    def _range_is_sensible(self):
        if self.start and self.end and self.end < self.start:
            raise ValueError("The end date is before the start date.")
        if self.start and self.end and (self.end - self.start).days > 366:
            raise ValueError("Ask for at most a year at a time.")
        return self


class ScheduledRow(BaseModel):
    id: str
    platform: str
    excerpt: str = Field(max_length=300)
    scheduled_at: str
    approval_status: Literal["pending", "approved", "rejected"]


class ScheduledPage(BaseModel):
    items: list[ScheduledRow]
    next_cursor: Optional[str] = None


class WaitingRow(BaseModel):
    id: str
    platform: str
    excerpt: str = Field(max_length=300)
    created_at: str
    needs_review_reason: Optional[str] = None


class WaitingPage(BaseModel):
    items: list[WaitingRow]
    next_cursor: Optional[str] = None


class BrandRow(BaseModel):
    id: str
    name: str
    type: str
    is_default: bool


class Brands(BaseModel):
    brands: list[BrandRow]


class RunsIn(PageIn):
    status: Optional[Literal["queued", "running", "paused", "done", "failed", "cancelled"]] = None


class RunRow(BaseModel):
    id: str
    kind: str
    title: str
    status: Literal["queued", "running", "paused", "done", "failed", "cancelled"]
    progress_percent: Optional[int] = None
    href: Optional[str] = None
    can_retry: bool = False


class RunPage(BaseModel):
    items: list[RunRow]
    next_cursor: Optional[str] = None


class PostIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)


class PostDetail(BaseModel):
    id: str
    platform: str
    content: str
    approval_status: Literal["pending", "approved", "rejected"]
    publish_status: str
    scheduled_at: Optional[str] = None
    created_at: str


class TopAssetsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["image", "audio", "video"]
    limit: int = Field(10, ge=1, le=50)


class AssetRank(BaseModel):
    rank: int
    id: str
    title: str
    average_engagement_rate: float
    measured_posts: int


class TopAssets(BaseModel):
    items: list[AssetRank]


def _read(name: str, summary: str, input_model: type[BaseModel], output_model: type[BaseModel], source: str, *examples: str,
          routes: tuple[str, ...] = ()) -> ToolSpec:
    return ToolSpec(
        name=name, summary=summary, risk=Risk.READ, scope=Scope.READ, mode=Mode.INSTANT, input_model=input_model,
        output_model=output_model, source=f"query:{source}", examples=examples, routes=routes,
    )


def specs() -> list[ToolSpec]:
    return [
        _read("me.whoami", "Show which workspace and role this connection is acting in, and the plan.", NoInput, WhoAmI, "whoami",
              "Who am I signed in as?"),
        _read("credits.summary", "Show this month's credits: the allowance, what is used and what is left.", NoInput, CreditsSummary,
              "credits.summary", "How many credits do I have left?"),
        _read("credits.prices", "List what each action costs in credits.", NoInput, Prices, "credits.costs", "How much does a picture cost?"),
        _read("plan.limits", "Show the plan's limits (pictures a day, campaigns running, keys and more) and how much is used.", NoInput,
              PlanLimits, "entitlements.limits", "How many pictures can I make today?"),
        _read("library.posts", "List posts in the Library, newest first, with an optional platform, stage (drafting, scheduled, published and so on) or search words.", ListPostsIn,
              PostPage, "library.posts", "Show my scheduled LinkedIn posts", routes=("GET /api/v1/content/pieces",)),
        _read("campaigns.list", "List campaigns, newest first, with an optional status.", ListCampaignsIn, CampaignPage, "campaigns.list",
              "Which campaigns are running?", routes=("GET /api/v1/campaigns/",)),
        _read("library.post", "Show one post in full: its text, approval and publishing status and time.", PostIn, PostDetail, "library.post",
              "Show me post 123", routes=("GET /api/v1/content/pieces/{piece_id}",)),
        _read("approvals.waiting", "List posts waiting for a person to approve them, with the reason when one was held for review.",
              WaitingIn, WaitingPage, "approvals.waiting", "What is waiting for my approval?"),
        _read("calendar.upcoming", "List posts scheduled to go out, soonest first, between two dates.", CalendarIn, ScheduledPage,
              "calendar.upcoming", "What is going out next week?"),
        _read("brands.list", "List the workspace's brand voices.", NoInput, Brands, "brands.list", "Which brand voices do I have?", routes=("GET /api/v1/brand/",)),
        _read("runs.list", "List recent background work (writing, pictures, videos) and how far along each is.", RunsIn, RunPage,
              "runs.list", "Is my video finished?", routes=("GET /api/v1/runs",)),
        _read("analytics.top_assets", "Rank the pictures, recordings or videos whose posts did best.", TopAssetsIn, TopAssets,
              "analytics.top_assets", "Which of my pictures worked best?",
              routes=("GET /api/v1/analytics/assets/{asset_type}/top",)),
    ]
