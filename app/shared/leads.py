"""Small helpers shared by the public forms and the staff view of leads."""

from datetime import datetime, timezone
from uuid import uuid4

from app.db.mongo import lead_counters, lead_events


async def next_contact_reference() -> str:
    """The next reference for a contact message, such as C-0042. The counter is atomic, so two messages never share one."""
    doc = await lead_counters.find_one_and_update({"_id": "contact"}, {"$inc": {"value": 1}}, upsert=True, return_document=True)
    return f"C-{int(doc['value']):04d}"


async def record_event(kind: str, target_id: str, action: str, user: dict, detail: str = "") -> None:
    """Write one line to the lead audit trail: who did what, to which lead or message, and when."""
    await lead_events.insert_one(
        {
            "id": str(uuid4()),
            "kind": kind,
            "target_id": target_id,
            "action": action,
            "actor_id": user.get("id", ""),
            "actor_name": user.get("name") or user.get("email") or "Staff",
            "detail": detail[:300],
            "at": datetime.now(timezone.utc),
        }
    )
