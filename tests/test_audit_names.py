"""A change history says who and what in words, never an id."""
from __future__ import annotations

from app.db.mongo import get_db
from app.shared.audit_names import readable


async def test_ids_in_a_history_become_names_and_unknown_ones_stay():
    db = get_db()
    await db["workspaces"].insert_one({"id": "ws_an_00000001", "name": "Acme Studio"})
    await db["users"].insert_one({"id": "us_an_00000001", "name": "", "email": "priya@example.com"})
    try:
        rows = await readable([
            {"target": "ws_an_00000001", "target_name": "ws_an_00000001", "by": "us_an_00000001"},
            {"target": "us_an_00000001", "target_name": None},
            {"target": "prices", "target_name": "Prices"},
            {"target": "ws_gone_0000001", "target_name": "ws_gone_0000001"},
        ])
        assert rows[0]["target_name"] == "Acme Studio" and rows[0]["by_email"] == "priya@example.com"
        assert rows[1]["target_name"] == "priya@example.com"
        assert rows[2]["target_name"] == "Prices"
        assert rows[3]["target_name"] == "ws_gone_0000001"
    finally:
        await db["workspaces"].delete_one({"id": "ws_an_00000001"})
        await db["users"].delete_one({"id": "us_an_00000001"})
