"""Shareable text for an issue or the overall status. Plain words, times labelled with their zone, no prompt or
user content, and always the issue number and a link back."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.shared.llm_health import catalogue, issues


def _when(dt: datetime | None, tz_label: str = "UTC") -> str:
    if not dt:
        return "unknown"
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%d %b %H:%M") + f" {tz_label}"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def issue_report(issue: dict[str, Any], *, link: str, reset_at: datetime | None = None) -> str:
    kind = catalogue.kind_for(issue.get("error_type", "unknown"))
    provider = issue.get("provider") or "The provider"
    model = issue.get("model") or "the model"
    features = issue.get("features") or []
    paths = issue.get("prompt_paths") or []
    fallback = {"working": "working", "partial": "partly working", "none": "none"}.get(issue.get("fallback_outcome", "none"), "unknown")
    render = lambda t: catalogue.render(t, provider=provider, model=model, feature=features[0] if features else "this feature", prompt_path=paths[0] if paths else None)  # noqa: E731
    lines = [
        f"{issues.issue_number_label(int(issue.get('number', 0)))}: {issues.title_for(issue)}",
        f"Status: {issue.get('status', 'open')}. Priority: {issue.get('priority', 'medium')}. First seen {_when(issue.get('first_seen'))}, last seen {_when(issue.get('last_seen'))}.",
        f"Impact: {_plural(int(issue.get('failed_request_count', 0)), 'failed request')} across {_plural(len(issue.get('affected_workspaces') or []), 'workspace')}"
        + (f" ({', '.join(features)})." if features else "."),
        f"Fallback: {fallback}.",
        f"Cause: {render(kind.cause)}",
        f"What to do: {render(kind.action_staff)}",
        f"For the dev team: {render(kind.action_dev)}",
    ]
    if reset_at:
        lines.append(f"Resets at {_when(reset_at)}.")
    technical = [f"provider={provider}", f"model={model}", f"type={issue.get('error_type')}"]
    if features:
        technical.append("feature=" + ",".join(features))
    if paths:
        technical.append("prompt=" + ",".join(paths[:5]))
    if issue.get("last_app_version"):
        technical.append(f"version={issue['last_app_version']}")
    lines.append("Technical: " + ", ".join(technical))
    if issue.get("notes"):
        lines.append(f"Latest note: {issue['notes'][-1].get('text', '')[:200]}")
    lines.append(f"Link: {link}")
    return "\n".join(lines)


def status_summary(banner: dict[str, str], providers: dict[str, str], open_issues: list[dict[str, Any]], *, link: str) -> str:
    lines = [f"LLM status: {banner['text']}"]
    for name, state in providers.items():
        lines.append(f"- {name.capitalize()}: {state.replace('_', ' ')}")
    if open_issues:
        lines.append("Open issues:")
        for i in open_issues[:5]:
            lines.append(f"- {issues.issue_number_label(int(i.get('number', 0)))} ({i.get('priority', 'medium')}): {issues.title_for(i)}")
    else:
        lines.append("No open issues.")
    lines.append(f"Link: {link}")
    return "\n".join(lines)
