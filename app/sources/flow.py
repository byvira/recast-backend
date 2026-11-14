"""What happens to the items a source gives: skip what was seen, skip what is off-topic, keep to the limits, then hand each one to the writer as a draft.

A source never writes or publishes by itself. It makes the same tool calls a person's key could make (`library.create_draft`, `text.generate`) through the
gatekeeper, as a `source` credential that can only read and create. So the price, the plan limits, the caps, the safety checks, the ledger and the undo
history are the ones every other way in already has, and a source can never publish, approve or delete: it is not allowed to, whatever an item says.

Item text comes from the internet. It is fenced as data (`app.ask.fence`) before it reaches the writer, and the draft always carries where it came from."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from app.ask import fence
from app.db.mongo import get_db
from app.sources.items import Item

logger = logging.getLogger(__name__)

MODES = ("link", "write")                       # link: a free saved draft with the link; write: the text pipeline writes a post from the item
THRESHOLDS = {"everything": 0.0, "medium": 0.30, "close": 0.45}        # provisional cosine cut-offs, tuned from the keep rate once there is real use
DEFAULT_DAILY_PER_SOURCE = 10
DEFAULT_DAILY_PER_WORKSPACE = 30
MAX_PER_PASS = 5
BRIEF_CHARS = 1500

RunTool = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
Relevance = Callable[[str, str], Awaitable[Optional[float]]]


def credential_id(workspace_id: str, user_id: str) -> str:
    return f"source:{workspace_id}:{user_id}"


def items_col():
    return get_db()["source_items"]


async def create_indexes() -> None:
    try:
        await items_col().create_index([("workspace_id", 1), ("item_id", 1)], unique=True)
        await items_col().create_index([("source_id", 1), ("seen_at", -1)])
        await items_col().create_index("seen_at", expireAfterSeconds=60 * 24 * 3600)
    except Exception as exc:  # noqa: BLE001 - optional
        logger.warning("Could not create the source item indexes: %s", exc)


@dataclass
class Result:
    drafted: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    failed: int = 0
    notes: list[str] = field(default_factory=list)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


def attribution(source: dict[str, Any], item: Item) -> str:
    name = source.get("name") or "the source"
    return f"Source: {name}" + (f" ({item.link})" if item.link else "")


def draft_text(source: dict[str, Any], item: Item) -> str:
    """The free saved draft: the item's title and its short summary, then where it came from. Words are cut so it is a pointer, not a copy of the article."""
    body = item.text[:280].rsplit(" ", 1)[0].rstrip(",;:") + ("…" if len(item.text) > 280 else "") if item.text else ""
    return "\n\n".join(p for p in (item.title.strip(), body, attribution(source, item)) if p)[:3000]


def brief(source: dict[str, Any], item: Item) -> str:
    """What the writer is given: the item as fenced data, with an instruction to write a short take in the brand's voice and to credit the source."""
    return (
        "Write a short post in the brand's voice that gives your own take on the item below and says where it came from. Do not copy its sentences. "
        f"{fence.RULES}\n\nItem title: {fence.clean(item.title, 200)}\n"
        f"Item text:\n{fence.fence(item.text, BRIEF_CHARS)}\nCredit: {attribution(source, item)}"
    )


def _today() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


async def drafted_today(workspace_id: str, source_id: Optional[str] = None) -> int:
    query: dict[str, Any] = {"workspace_id": workspace_id, "outcome": "drafted", "seen_at": {"$gte": _today()}}
    if source_id:
        query["source_id"] = source_id
    return await items_col().count_documents(query)


async def record(source: dict[str, Any], item: Item, outcome: str, **extra: Any) -> None:
    try:
        await items_col().update_one(
            {"workspace_id": source["workspace_id"], "item_id": item.id},
            {"$setOnInsert": {"workspace_id": source["workspace_id"], "item_id": item.id, "source_id": source["id"], "title": item.title[:200], "link": item.link, "seen_at": datetime.now(timezone.utc)},
             "$set": {"outcome": outcome, **extra}}, upsert=True)
    except Exception as exc:  # noqa: BLE001 - a record that cannot be saved must not stop the run
        logger.warning("Could not record a source item: %s", exc)


async def already_seen(workspace_id: str, item_id: str) -> bool:
    return await items_col().count_documents({"workspace_id": workspace_id, "item_id": item_id}, limit=1) > 0


async def relevant(source: dict[str, Any], item: Item, brand_text: str, score: Relevance) -> bool:
    """True when the item is close enough to the brand. With no embedding service it falls back to shared words, and when nothing can be judged it lets the item through:
    a gate that cannot see must not silently drop everything."""
    cut = THRESHOLDS.get(source.get("relevance", "medium"), THRESHOLDS["medium"])
    if cut <= 0 or not brand_text.strip():
        return True
    value = await score(brand_text, f"{item.title}. {item.text[:600]}")
    return True if value is None else value >= cut


async def process(source: dict[str, Any], items: list[Item], *, run_tool: RunTool, brand_text: str, score: Relevance, first_look: bool = False,
                  now: Optional[datetime] = None) -> Result:
    """Handle one pass over a source. `first_look` is the pass right after a source is added: items already published before then are not drafted (no flood of old posts)."""
    out = Result()
    now = now or datetime.now(timezone.utc)
    per_source = int(source.get("daily_limit") or DEFAULT_DAILY_PER_SOURCE)
    per_workspace = int(source.get("workspace_daily_limit") or DEFAULT_DAILY_PER_WORKSPACE)
    cutoff = _aware(source.get("created_at")) or now
    mode = source.get("mode", "link")
    for item in items[: MAX_PER_PASS * 4]:
        if out.drafted >= MAX_PER_PASS:
            out.skip("pass_limit")
            break
        if await already_seen(source["workspace_id"], item.id):
            out.skip("duplicate")
            continue
        if item.published and _aware(item.published) < cutoff - timedelta(minutes=1) and not source.get("include_older"):
            await record(source, item, "skipped", reason="older")
            out.skip("older")
            continue
        if await drafted_today(source["workspace_id"], source["id"]) >= per_source:
            out.skip("source_daily_limit")
            out.notes.append("This source reached its limit for today. The rest waits for tomorrow.")
            break
        if await drafted_today(source["workspace_id"]) >= per_workspace:
            out.skip("workspace_daily_limit")
            out.notes.append("This workspace reached its limit for source drafts today.")
            break
        if not await relevant(source, item, brand_text, score):
            await record(source, item, "skipped", reason="off_topic")
            out.skip("off_topic")
            continue
        platform = (source.get("platforms") or ["LinkedIn"])[0]
        try:
            if mode == "write":
                answer = await run_tool("text.generate", {"source_type": "text", "content": brief(source, item), "platforms": list(source.get("platforms") or [platform]),
                                                          "brand_id": source["brand_id"], "publish_targets": []})
            else:
                answer = await run_tool("library.create_draft", {"platform": platform, "brand_id": source["brand_id"], "content": draft_text(source, item)})
        except SourceToolError as exc:
            out.failed += 1
            await record(source, item, "failed", reason=exc.code)
            out.notes.append(exc.message)
            if exc.stop:                                      # out of credits, switched off, or no longer allowed: stop this pass, the rest waits
                break
            continue
        await record(source, item, "drafted", mode=mode, result=_short(answer))
        out.drafted += 1
    return out


class SourceToolError(Exception):
    """A tool refused or failed. `stop` is True when the rest of the pass cannot work either (credits, a switch, a limit)."""

    def __init__(self, code: str, message: str, stop: bool = False):
        super().__init__(message)
        self.code, self.message, self.stop = code, message, stop


def _short(answer: Any) -> Any:
    try:
        data = answer.get("data") if isinstance(answer, dict) else None
        run = answer.get("run") if isinstance(answer, dict) else None
        return {"run": (run or {}).get("id") if isinstance(run, dict) else run, "piece": (data or {}).get("piece_id") if isinstance(data, dict) else None}
    except Exception:  # noqa: BLE001
        return None


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
