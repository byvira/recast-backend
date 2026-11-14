"""What Ekavo reads of a conversation before it answers: a short, budgeted, fenced block.

It holds the running summary, the last few messages word for word, a line of what Ekavo just made (ids and titles only, so "make it shorter" can point
at the right draft) and a few facts about the workspace. Everything in it is a person's or Ekavo's earlier words, so the whole block is fenced as data, never
as instructions (see `fence.py`), and it is cut to a character budget (about 1,200 tokens) newest first, so the latest turn is never the part that is dropped."""
from __future__ import annotations

from typing import Any, Optional

from app.ask import fence
from app.ask.threads import RECENT, SUMMARY_CHARS

BUDGET_CHARS = 4800                 # about 1,200 tokens
MESSAGE_CHARS = 500
FACT_CHARS = 160
MAX_REFS = 6


def _who(message: dict[str, Any]) -> str:
    return "Person" if (message.get("author") or {}).get("type") == "member" else "Ekavo"


def _made(message: dict[str, Any]) -> Optional[str]:
    """"Made: library.create_draft piece p1 (A launch post)" for a message that carries what was made."""
    refs = message.get("refs") or {}
    made = refs.get("made")
    if not isinstance(made, list) or not made:
        return None
    parts = []
    for item in made[:MAX_REFS]:
        if isinstance(item, dict) and item.get("id"):
            parts.append(f"{fence.clean(str(item.get('kind') or 'item'), 20)} {fence.clean(str(item['id']), 40)}" + (f" ({fence.clean(str(item.get('title')), 60)})" if item.get("title") else ""))
    return ("Made: " + "; ".join(parts)) if parts else None


def build(thread: dict[str, Any], recent: list[dict[str, Any]], facts: Optional[dict[str, Any]] = None, *, budget: int = BUDGET_CHARS) -> str:
    """The fenced conversation block, or an empty string for a thread with nothing in it yet."""
    head: list[str] = []
    for key, value in (facts or {}).items():
        text = fence.clean(f"{key}: {value}", FACT_CHARS)
        if text:
            head.append(text)
    summary = fence.clean(thread.get("summary") or "", SUMMARY_CHARS)
    if summary:
        head.append(f"Earlier in this conversation: {summary}")
    lines: list[str] = []
    for message in recent[-RECENT:]:
        text = fence.clean(message.get("text") or "", MESSAGE_CHARS)
        if text:
            lines.append(f"{_who(message)}: {text}")
        made = _made(message)
        if made:
            lines.append(made)
    if not head and not lines:
        return ""
    fixed = "\n".join(head)
    room = max(0, budget - len(fixed) - 40)
    kept: list[str] = []
    for line in reversed(lines):                      # newest first, so a long history loses its oldest lines
        if len(line) + 1 > room:
            break
        kept.append(line)
        room -= len(line) + 1
    body = "\n".join(([fixed] if fixed else []) + list(reversed(kept)))
    return fence.fence(body, budget)


_ID_KEYS = ("piece_id", "id", "asset_id", "campaign_id")
_TITLE_KEYS = ("title", "name")


def made_from_result(result: dict[str, Any]) -> list[dict[str, str]]:
    """What a finished run made, as short {kind, id, title} entries, from the steps that changed something. Reads are skipped: they made nothing."""
    from app.tools.contract import Risk
    from app.tools.registry import get_registry

    registry = get_registry()
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for step in (result or {}).get("steps") or []:
        if step.get("state") != "done":
            continue
        spec = registry.get(str(step.get("tool") or ""))
        if spec is None or spec.risk is Risk.READ:
            continue
        data = ((step.get("result") or {}).get("data")) or {}
        pools = [data, *[v for v in data.values() if isinstance(v, dict)]] if isinstance(data, dict) else []
        for pool in pools:
            ident = next((str(pool[k]) for k in _ID_KEYS if isinstance(pool.get(k), (str, int)) and str(pool[k])), None)
            if ident and ident not in seen:
                seen.add(ident)
                title = next((str(pool[k]) for k in _TITLE_KEYS if isinstance(pool.get(k), str) and pool[k].strip()), "")
                out.append({"kind": str(step.get("tool")).split(".")[0], "id": ident[:64], "title": fence.clean(title, 60)})
                break
        if len(out) >= MAX_REFS:
            break
    return out
