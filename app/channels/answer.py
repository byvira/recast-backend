"""A short written answer from what a read finished with, so a question in a chat gets its answer and not only "open the app".

Plain code, no model, nothing charged. It only reads steps that cannot change anything (read tools), keeps to a few short lines, leaves out anything that
looks like a secret, and never copies a long text (a draft's own words stay in the app). Labels are the tool's own field names, tidied; a later step can
have a model explain them in the person's language."""
from __future__ import annotations

import re
from typing import Any

from app.tools.contract import Risk
from app.tools.registry import get_registry

MAX_CHARS = 700
MAX_LINES = 14
MAX_VALUE = 120
_SECRET = re.compile(r"token|secret|password|hash|api_?key|authorization|signature", re.IGNORECASE)
_TITLES = ("title", "name", "label", "summary", "message")


def _label(key: str) -> str:
    return str(key).replace("_", " ").strip().capitalize()


def _scalar(value: Any) -> str | None:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:,}" if isinstance(value, int) else f"{value:g}"
    if isinstance(value, str) and 0 < len(value.strip()) <= MAX_VALUE:
        return value.strip()
    return None


def _lines(data: Any, prefix: str = "", depth: int = 0) -> list[str]:
    out: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if _SECRET.search(str(key)):
                continue
            text = _scalar(value)
            if text is not None:
                out.append(f"{prefix}{_label(key)}: {text}")
            elif isinstance(value, dict) and depth < 1:
                inner = _lines(value, "  ", depth + 1)
                if inner:
                    out.append(f"{prefix}{_label(key)}:")
                    out.extend(inner)
            elif isinstance(value, list) and value:
                out.append(f"{prefix}{_label(key)}: {_list_line(value)}")
    elif isinstance(data, list):
        out.append(_list_line(data))
    return out


def _list_line(items: list[Any]) -> str:
    """"3 items: First, Second, Third", using each item's title or name when it has one."""
    names: list[str] = []
    for item in items[:5]:
        if isinstance(item, dict):
            title = next((_scalar(item[k]) for k in _TITLES if k in item and _scalar(item[k])), None)
            if title:
                names.append(title)
        elif _scalar(item):
            names.append(str(_scalar(item)))
    count = f"{len(items)} {'item' if len(items) == 1 else 'items'}"
    return f"{count}: {', '.join(names)}" if names else count


def short_answer(run: dict[str, Any]) -> str:
    """The answer to show, or an empty string when the run did more than read (those get a link to the result) or there is nothing to say."""
    steps = (run.get("result") or {}).get("steps") or []
    done = [s for s in steps if s.get("state") == "done"]
    if not done:
        return ""
    registry = get_registry()
    for step in done:
        spec = registry.get(str(step.get("tool") or ""))
        if spec is None or spec.risk is not Risk.READ:
            return ""
    lines: list[str] = []
    for step in done:
        data = (step.get("result") or {}).get("data")
        lines.extend(_lines(data))
    lines = lines[:MAX_LINES]
    text = "\n".join(lines)
    return text[:MAX_CHARS].rstrip()
