"""Static per-tier default configuration, snapshotted onto Workspace at creation."""

from app.models.workspace import WorkspaceTier, TierConfig

TIER_DEFAULTS: dict[WorkspaceTier, TierConfig] = {
    WorkspaceTier.SINGLE: TierConfig(seats=1, storage_limit_gb=5, settings={}),
    WorkspaceTier.DUO: TierConfig(seats=2, storage_limit_gb=20, settings={}),
    WorkspaceTier.LARGE: TierConfig(seats=10, storage_limit_gb=100, settings={}),
}