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