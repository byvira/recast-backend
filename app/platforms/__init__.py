"""
app.platforms — the platform registry.

Layout:
    base.py       PlatformDefinition, register_platform, PLATFORM_REGISTRY, import_all()
    linkedin.py, instagram.py, threads.py, facebook.py, bluesky.py
                  The 5 real, code-driven platforms. Started as pure
                  consolidation of app/pipelines/publish/registry.py
                  (PUBLISHERS), validators.py (VALIDATORS) and
                  app/pipelines/analytics/aggregator.py (_FETCHERS) — those
                  three dicts have since been removed; get_publisher(),
                  validate_for_platform() and the analytics fetcher dispatch
                  all now resolve through PLATFORM_REGISTRY (via each
                  PlatformDefinition's publisher_cls/validator_fn/
                  analytics_fetcher_cls dotted paths), verified to preserve
                  every call site's exact prior behavior (error messages,
                  the reddit-validator-with-no-publisher edge case, the
                  6-platform analytics default-fetch set). A new platform is
                  now declared once, here, not duplicated across 3 dicts.
    planned/      The other 66 platforms from the source platform directory
                  (docs/PLATFORM_REGISTRY_PLAN.md's "71" — see planned/__init__.py
                  for the category breakdown), metadata only, status="planned"
                  or "partial". No publisher/validator classes exist for these
                  yet — building one is what flips status to "active".

Call app.platforms.base.import_all() once at startup (done in app/main.py) so
every module's registration side effect has run before anything reads
PLATFORM_REGISTRY.

On TWITTER_THREAD: the plan calls for "folding TWITTER_THREAD into a shape-flag
on TWITTER". The registry does exactly that — the "twitter" PlatformDefinition
carries shapes=["post", "thread"], so anything new (the /api/v1/platforms
response, a future frontend platform picker) sees one Twitter/X platform with
two content shapes, not two platforms. What this deliberately does NOT do is
migrate the live text-generation pipeline off Platform.TWITTER_THREAD as a
distinct enum member — PLATFORM_RULES/HASHTAG_RULES/CTA_RULES in
app/pipelines/text/generator.py, plus chips.py/repurpose.py/quality.py/
normalizer.py/scorer.py and 5 frontend files, all key off it today, and
docs/FINDINGS.md's Q1/Unknowns pass already confirmed it's two fully-wired,
working enum members, not a bug. Migrating those ~13 call sites to read a
shape flag instead is real, separate surgery on a currently-correct pipeline —
left for its own pass rather than bundled into this registry foundation.
"""
