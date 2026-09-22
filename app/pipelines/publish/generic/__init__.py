"""
Generic publishers for config-driven platforms — consume platform_configs
instead of a per-platform OAuth token, so adding a new webhook or
manual-handoff platform needs zero new Python code, only a PlatformDefinition
(app/platforms/planned/) plus an admin filling in the Ops Dashboard form.

Deliberately NOT wired into app/pipelines/publish/registry.py's get_publisher()
or the live /publish/now path yet — that's cross-cutting surgery on the
currently-working publish flow (touches how PUBLISHERS is looked up, how
PublishRequest carries a workspace_connections token vs. a platform_configs
row) that belongs in its own reviewed pass, not bundled into standing up the
Ops Dashboard's data layer. These classes are real and independently usable
today; wiring them into the orchestrator is follow-up work.
"""
