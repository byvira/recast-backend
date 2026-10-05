"""A member can get a copy of their support data; staff-only notes and storage paths are never part of it."""
from datetime import datetime, timezone
from itertools import count
from uuid import uuid4

from app.db.mongo import support_tickets
from app.shared import support_privacy
from tests.conftest import create_workspace


_NUMBERS = count(900_000)


def _ticket(ws_id: str, user_id: str, **extra) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": f"t-{uuid4()}", "number": next(_NUMBERS), "workspace_id": ws_id, "workspace_name": "WS", "created_by": user_id,
        "created_by_name": "Asha", "subject": "My post did not go out", "category": "Publishing", "severity": "normal",
        "status": "open", "created_at": now, "updated_at": now,
        "messages": [
            {"sender": "member", "sender_name": "Asha", "text": "It never posted.", "created_at": now, "is_internal": False,
             "attachments": [{"name": "screen.png", "size": 1234, "storage_key": "private/abc/screen.png"}]},
            {"sender": "staff", "sender_name": "Ravi", "text": "SECRET INTERNAL NOTE", "created_at": now, "is_internal": True},
            {"sender": "staff", "sender_name": "Ravi", "text": "Fixed, please try again.", "created_at": now, "is_internal": False},
        ],
        **extra,
    }


async def test_the_export_has_the_members_tickets_and_messages_without_internal_notes_or_storage_paths():
    user_id = f"u-{uuid4()}"
    await support_tickets.insert_one(_ticket(f"ws-{uuid4()}", user_id))
    await support_tickets.insert_one(_ticket(f"ws-{uuid4()}", f"other-{uuid4()}"))          # somebody else's

    data = await support_privacy.export_member_data(user_id)

    assert len(data["tickets"]) == 1
    ticket = data["tickets"][0]
    texts = [m["text"] for m in ticket["messages"]]
    assert texts == ["It never posted.", "Fixed, please try again."]
    assert "SECRET INTERNAL NOTE" not in repr(data) and "storage_key" not in repr(data) and "private/abc" not in repr(data)
    assert ticket["messages"][0]["attachments"] == [{"name": "screen.png", "size": 1234}]
    assert ticket["subject"] == "My post did not go out" and ticket["erased"] is False


async def test_an_erased_ticket_is_exported_as_erased_with_no_words():
    user_id = f"u-{uuid4()}"
    doc = _ticket(f"ws-{uuid4()}", user_id)
    await support_tickets.insert_one(doc)
    await support_privacy.anonymize_ticket(doc["id"])

    data = await support_privacy.export_member_data(user_id)

    ticket = data["tickets"][0]
    assert ticket["erased"] is True
    assert all(m["text"] == support_privacy.REMOVED_TEXT and m["removed"] for m in ticket["messages"])


def test_the_csv_has_one_row_per_message_and_makes_formula_cells_plain_text():
    data = {"tickets": [
        {"number": 1, "id": "t1", "subject": "S", "category": "C", "status": "open", "created_at": "2026-10-05",
         "messages": [{"from": "member", "name": "A", "sent_at": "x", "text": "=HYPERLINK(\"http://evil\")"},
                      {"from": "staff", "name": "B", "sent_at": "y", "text": "Hello"}]},
        {"number": 2, "id": "t2", "subject": "Empty", "category": "C", "status": "closed", "created_at": "2026-10-06", "messages": []},
    ]}

    rows = support_privacy.export_to_csv(data).strip().splitlines()

    assert rows[0].startswith("ticket_number,ticket_id,subject")
    assert len(rows) == 1 + 2 + 1                                      # header, two messages, one ticket with none
    assert "'=HYPERLINK" in rows[1] and ",=HYPERLINK" not in rows[1]


async def test_a_member_can_download_their_own_data_as_json_or_csv(signup_user):
    client, profile = await signup_user()
    ws_id = await create_workspace(client, "Export WS")
    await support_tickets.insert_one(_ticket(ws_id, profile["id"]))
    headers = {"X-Workspace-Id": ws_id}

    res = await client.get("/api/v1/support/my-data/export", headers=headers)
    assert res.status_code == 200, res.text
    assert len(res.json()["tickets"]) == 1 and "SECRET INTERNAL NOTE" not in res.text

    csv_res = await client.get("/api/v1/support/my-data/export?format=csv", headers=headers)
    assert csv_res.status_code == 200 and csv_res.headers["content-type"].startswith("text/csv")
    assert "It never posted." in csv_res.text and "SECRET INTERNAL NOTE" not in csv_res.text
