"""Layer 1 — the per-member personal assistant ("Remy").

Consumes ``content.created`` / ``content.updated`` events from the
``recast:events`` Redis stream (via the arq worker), maintains one
``member_personas`` document per (workspace_id, user_id), and emits
``assistant.signal`` events on voice drift, unusual volume, topic shift and
quality regression.

Pipeline-agnostic: the only place that knows a pipeline's storage layout is
``history.PIPELINE_SOURCES``. Everything else works off the medium-neutral
``content_text`` on the event payload.
"""
