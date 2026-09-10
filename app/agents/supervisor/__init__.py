"""Layer 2 — the workspace supervisor ("Odette").

A standalone LangGraph agent that reasons over *aggregated* workspace signal, not
a 1:1 event listener. It runs entirely in the arq worker on two cron cadences:

  * ``supervisor_rules_tick``  — every 1 min, deterministic hard-limit flags
  * ``supervisor_reason_tick`` — every 5 min, debounced LLM reasoning pass

Everything it produces (insights, flags, notifications) is visible only to
workspace owners/admins, enforced by the existing permission-set RBAC
(``view_workspace_insights``).

Cross-workspace isolation: every query is scoped by a ``workspace_id`` that
comes from the stream partition / authenticated context, never from model
output. The reasoning tools are closures bound to that id — the model cannot
name another workspace.
"""
