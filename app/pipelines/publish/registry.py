"""
Publisher registry.
Maps platform names to publisher classes.
Adding a new platform = create class + add one line here.
"""

from app.pipelines.publish.base import PlatformPublisher
from app.pipelines.publish.linkedin.publisher import LinkedInPublisher
from app.pipelines.publish.bluesky.publisher import BlueSkyPublisher
from app.pipelines.publish.meta.instagram import InstagramPublisher
from app.pipelines.publish.meta.threads import ThreadsPublisher
from app.pipelines.publish.meta.facebook import FacebookPublisher

PUBLISHERS: dict[str, type[PlatformPublisher]] = {
    "linkedin": LinkedInPublisher,
    
    # Phase 3 — uncomment when built
    "instagram": InstagramPublisher,
    "threads":   ThreadsPublisher,
    "facebook":  FacebookPublisher,
    # Phase 4 — uncomment when built
    # "reddit":    RedditPublisher,
    "bluesky":   BlueSkyPublisher,
}


def get_publisher(platform: str) -> PlatformPublisher:
    """
    Get publisher instance for a platform.
    Raises ValueError if platform not yet implemented.
    """
    publisher_class = PUBLISHERS.get(platform.lower())
    if not publisher_class:
        raise ValueError(
            f"Platform '{platform}' not supported yet. "
            f"Available: {', '.join(PUBLISHERS.keys()) or 'none'}"
        )
    return publisher_class()


def register_publisher(
    platform: str,
    publisher_class: type[PlatformPublisher],
) -> None:
    """Register a publisher class for a platform."""
    PUBLISHERS[platform.lower()] = publisher_class