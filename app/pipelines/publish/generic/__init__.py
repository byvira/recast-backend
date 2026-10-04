"""Generic publishers for config-driven platforms.

A webhook platform (Telegram, Slack, Discord, ...) or a manual-handoff platform (X, Reddit, ...) has no sign in, so
it needs no per-platform Python code: a PlatformDefinition in app/platforms/ plus the settings Ops saves for it
(platform_config_store). `adapter.ConfigPublisherAdapter` wraps WebhookPublisher or ManualHandoffPublisher and
returns the same PublishResult the real publishers return.

How it is reached: callers try registry.get_publisher() first (real publishers, unchanged) and, only when that
says "not supported", ask registry.adapter_for(). It returns an adapter only for a platform with a webhook or
manual-handoff pattern that has saved, enabled settings, so a platform with no settings stays "not supported yet".
Publish Now, the scheduled worker and the scheduling gate all resolve this way.
"""
