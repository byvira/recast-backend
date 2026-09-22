"""
Metadata-only platform registrations — status="planned" (or "partial" where a
sliver of real code already exists elsewhere in the codebase). Sourced from
the platform directory referenced in docs/PLATFORM_REGISTRY_PLAN.md Stage 1
item 3 ("the rest of the 71"), current as of 2026-09-20.

Confidence follows that source's own confidence table: "verified" only for the
4 platforms it checked against official pages that session (TikTok, YouTube,
Pinterest, Reddit — YouTube lives in app/platforms/youtube.py, not here, since
its analytics side is already real); "third_party" for the handful it flagged
as checked only via third-party summaries (X pricing, Apple Podcasts' feed
refresh interval, Buzzsprout/Transistor host features); "unverified" — the
large majority — for everything marked (verify) in the source, meaning it has
not been checked against that platform's own docs. Treat "unverified" entries
as directional, not load-bearing, until someone checks them before building.

Files, one per source-directory section:
    social.py            7 remaining social/feed networks (facebook, instagram,
                          threads, linkedin, bluesky, twitter are the 5 real +
                          1 partial platforms, registered at app/platforms/ top level)
    fediverse.py          3 — lemmy, pixelfed, peertube
    video.py               3 remaining video platforms (youtube is top-level, partial)
    messaging.py          11 — telegram, discord, slack, teams, google chat,
                                whatsapp (channels + business), line, kakaotalk,
                                discourse, circle
    regional.py            3 — wechat, weibo, vk (flagged for a later phase —
                                each needs language/legal/account-verification
                                review before any build)
    blogs_cms.py           11 — wordpress (self-hosted + .com counted separately
                                to match the source's "71" headline total), ghost,
                                medium, substack, dev.to, hashnode, blogger,
                                webflow, shopify blog, notion
    email.py                5 — resend, mailchimp, kit, beehiiv, buttondown
    podcasts.py             9 — audio directories reached via one shared RSS feed
    audio_hosts.py           6 — direct-upload audio hosts (the upgrade path
                                beyond serving Recast's own RSS feed)
    business_other.py       6 — google business profile, flickr, imgur,
                                hacker news, patreon, github
"""
