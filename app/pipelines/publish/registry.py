"""
Publisher registry.
Maps platform names to publisher classes.

Sourced from app.platforms.PLATFORM_REGISTRY (see app/platforms/base.py) —
adding a new platform means creating the publisher class and a
PlatformDefinition entry under app/platforms/ with publisher_cls pointing at
it, not editing a dict here. This file is now a thin, backward-compatible
wrapper so every existing call site (app/api/v1/publish.py, content.py,
oauth.py, app/workers/token_refresh.py, scheduled_posts.py — see
docs/PLATFORM_REGISTRY_PLAN.md's Stage 1/cleanup notes) keeps working
unchanged; only the lookup's source of truth moved.
"""

from app.pipelines.publish.base import PlatformPublisher
from app.platforms.base import get_platform as _get_platform_definition
from app.platforms.base import import_all as _import_all_platforms
from app.platforms.base import list_platforms as _list_platforms


def get_publisher(platform: str) -> PlatformPublisher:
    """
    Get publisher instance for a platform.
    Raises ValueError if platform not yet implemented.
    """
    _import_all_platforms()
    definition = _get_platform_definition(platform)
    publisher_class = definition.resolve_publisher_cls() if definition else None
    if not publisher_class:
        available = [p.key for p in _list_platforms() if p.publisher_cls]
        raise ValueError(
            f"Platform '{platform}' not supported yet. "
            f"Available: {', '.join(available) or 'none'}"
        )
    return publisher_class()

async def adapter_for(platform: str, workspace_id: str):
    """The thin adapter for a webhook or manual-handoff platform that has saved, enabled settings, else None.
    Callers try get_publisher first and only ask here when it says "not supported", so real publishers resolve
    exactly as before and a platform with no saved settings stays "not supported yet"."""
    from app.pipelines.publish.generic.adapter import ConfigPublisherAdapter, is_adapter_pattern
    from app.pipelines.publish.platform_config_store import get_effective_config

    _import_all_platforms()
    definition = _get_platform_definition(platform)
    if not is_adapter_pattern(definition):
        return None
    config = await get_effective_config(workspace_id, definition.key)
    return ConfigPublisherAdapter(definition, config) if config else None


async def resolve_publisher(platform: str, workspace_id: str):
    """get_publisher, then the adapter. Raises the same ValueError get_publisher does when neither applies."""
    try:
        return get_publisher(platform)
    except ValueError:
        adapter = await adapter_for(platform, workspace_id)
        if adapter is None:
            raise
        return adapter
