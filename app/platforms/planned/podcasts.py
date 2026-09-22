from app.platforms.base import PlatformDefinition, register_platform

# One shared RSS feed covers all nine — Recast publishes each episode once and
# these directories pick it up. Submit the feed to each directory once; after
# approval, new episodes appear automatically.
CATEGORY = "Audio directories via RSS"

ROWS = [
    dict(
        key="apple_podcasts", label="Apple Podcasts",
        pipelines=frozenset({"audio", "text", "image"}),
        native_formats={"audio": "native", "text": "show_notes", "image": "cover_art"},
        mode="config_driven", integration_pattern="rss_pull",
        rate_limits="Checks the feed for new episodes about every 24 hours (third-party summary, not confirmed on Apple's own docs).",
        access_notes="Free; submit the feed once via Apple Podcasts Connect. Largest podcast directory — many apps copy its listings.",
        confidence="third_party",
    ),
    dict(
        key="spotify", label="Spotify",
        pipelines=frozenset({"audio", "video"}),
        native_formats={"audio": "native", "video": "native"},
        mode="config_driven", integration_pattern="rss_pull",
        policy_constraints=["Submission goes through Spotify for Creators."],
        access_notes="Free; submit the feed once. Video podcasts supported on some hosts.",
        confidence="unverified",
    ),
    dict(
        key="amazon_music", label="Amazon Music",
        pipelines=frozenset({"audio"}),
        native_formats={"audio": "native"},
        mode="config_driven", integration_pattern="rss_pull",
        policy_constraints=["Separate podcaster submission process from Spotify/Apple."],
        access_notes="Free; submit the feed (verify current submission flow).",
        confidence="unverified",
    ),
    dict(
        key="youtube_music", label="YouTube Music",
        pipelines=frozenset({"audio"}),
        native_formats={"audio": "native"},
        mode="config_driven", integration_pattern="rss_pull",
        policy_constraints=["Also supports video episodes, listed next to the connected YouTube channel."],
        access_notes="Free; submit the feed (verify).",
        confidence="unverified",
    ),
    dict(
        key="iheartradio", label="iHeartRadio",
        pipelines=frozenset({"audio"}),
        native_formats={"audio": "native"},
        mode="config_driven", integration_pattern="rss_pull",
        policy_constraints=["Broad US radio/podcast audience."],
        access_notes="Free; submit the feed (verify).",
        confidence="unverified",
    ),
    dict(
        key="pocket_casts", label="Pocket Casts",
        pipelines=frozenset({"audio"}),
        native_formats={"audio": "native"},
        mode="config_driven", integration_pattern="rss_pull",
        policy_constraints=["Independent app; pulls from directories rather than a direct submission."],
        access_notes="Free; reached via directory listings, not a direct submission.",
        confidence="unverified",
    ),
    dict(
        key="podcast_addict", label="Podcast Addict",
        pipelines=frozenset({"audio"}),
        native_formats={"audio": "native"},
        mode="config_driven", integration_pattern="rss_pull",
        policy_constraints=["Large Android user base."],
        access_notes="Free; reached via directory listings.",
        confidence="unverified",
    ),
    dict(
        key="deezer", label="Deezer",
        pipelines=frozenset({"audio"}),
        native_formats={"audio": "native"},
        mode="config_driven", integration_pattern="rss_pull",
        policy_constraints=["Regional strength in France and Europe."],
        access_notes="Free; submit the feed (verify).",
        confidence="unverified",
    ),
    dict(
        key="tunein", label="TuneIn",
        pipelines=frozenset({"audio"}),
        native_formats={"audio": "native"},
        mode="config_driven", integration_pattern="rss_pull",
        policy_constraints=["Live-radio users also discover podcasts here."],
        access_notes="Free; submit the feed (verify).",
        confidence="unverified",
    ),
]

for _row in ROWS:
    register_platform(PlatformDefinition(category=CATEGORY, status="planned", **_row))
