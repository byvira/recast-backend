"""Support tickets, Phase 3: attachments, limits, paging, search and filters,
snooze, the lifecycle scheduler and workspace-admin visibility."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.db.mongo import support_tickets, users
from app.shared import storage
from app.shared import support_files as files_service
from app.shared import support_rules as rules
from app.workers.support_lifecycle import run_support_lifecycle
from tests.conftest import create_workspace, invite_and_accept, signup_new_user
from tests.test_support_tickets import TICKET, _file, _member, _staff, support_tickets_doc

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def fake_storage(monkeypatch):
    """No real Cloudinary calls: uploads return a made-up key, links are fake."""
    monkeypatch.setattr(
        storage, "upload_private_file", lambda data, folder, pid: f"recast/private/support/{folder}/{pid}"
    )
    monkeypatch.setattr(
        storage, "signed_private_url", lambda key, secs=300: f"https://signed.example/{key}?exp={secs}"
    )


async def _upload(client, name="shot.png", data=PNG, mime="image/png", path="/api/v1/support/uploads", **form):
    return await client.post(path, files={"file": (name, data, mime)}, data=form)


# ── Attachments ──────────────────────────────────────────────────────────────
async def test_attachment_round_trip_and_access_rules(make_client, fake_storage):
    member, _ = await _member(make_client)
    other, _ = await _member(make_client)
    staff, _ = await _staff(make_client)

    up = await _upload(member)
    assert up.status_code == 200, up.text
    file_id = up.json()["file_id"]
    created = await _file(member, attachment_ids=[file_id])
    tid = created["id"]
    assert created["messages"][0]["attachments"][0]["file_id"] == file_id

    link = await member.get(f"/api/v1/support/attachments/{file_id}")
    assert link.status_code == 200
    assert link.json()["url"].startswith("https://signed.example/")
    assert link.json()["expires_in"] == rules.SIGNED_URL_SECONDS

    # Someone else's file is not reachable, and neither is one nobody attached.
    assert (await other.get(f"/api/v1/support/attachments/{file_id}")).status_code == 404
    loose = (await _upload(member)).json()["file_id"]
    assert (await member.get(f"/api/v1/support/attachments/{loose}")).status_code == 404
    assert (await staff.get(f"/api/v1/ops/support/attachments/{file_id}")).status_code == 200

    # A file staff attach to an internal note never reaches the member.
    staff_file = (await _upload(staff, path="/api/v1/ops/support/uploads")).json()["file_id"]
    await staff.post(
        f"/api/v1/ops/support/tickets/{tid}/messages",
        json={"text": "internal", "is_internal": True, "attachment_ids": [staff_file]},
    )
    assert (await member.get(f"/api/v1/support/attachments/{staff_file}")).status_code == 404
    assert (await staff.get(f"/api/v1/ops/support/attachments/{staff_file}")).status_code == 200


async def test_attachment_type_and_size_rules(make_client, fake_storage, monkeypatch):
    member, _ = await _member(make_client)

    assert (await _upload(member, "run.exe", b"MZ\x90\x00", "application/octet-stream")).status_code == 422
    fake = await _upload(member, "pretend.png", b"MZ\x90\x00 not an image", "image/png")
    assert fake.status_code == 422
    assert "match" in fake.json()["detail"]
    assert (await _upload(member, "a.txt", b"hello\x00world", "text/plain")).status_code == 422
    assert (await _upload(member, "e.txt", b"", "text/plain")).status_code == 422

    monkeypatch.setattr(files_service, "MAX_ATTACHMENT_BYTES", 100)
    monkeypatch.setattr(rules, "MAX_ATTACHMENT_BYTES", 100)
    assert (await _upload(member, "big.txt", b"a" * 500, "text/plain")).status_code == 422
    monkeypatch.undo()

    assert (await _upload(member, "notes.log", b"line one\nline two\n", "text/plain")).status_code == 200


async def test_blocked_file_warns_staff_and_message_still_saves(make_client, fake_storage):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member))["id"]

    bad = await _upload(member, "evil.png", b"not a png at all", "image/png", ticket_id=tid)
    assert bad.status_code == 422

    assert (await member.post(f"/api/v1/support/tickets/{tid}/messages", json={"text": "sent anyway"})).status_code == 200
    detail = (await staff.get(f"/api/v1/ops/support/tickets/{tid}")).json()
    assert any("blocked" in m["text"] and m["is_internal"] for m in detail["ticket"]["messages"])
    assert "attachment_blocked" in [e["type"] for e in detail["events"]]
    assert "blocked" not in (await member.get(f"/api/v1/support/tickets/{tid}")).text


async def test_cannot_attach_someone_elses_or_too_many_files(make_client, fake_storage):
    a, _ = await _member(make_client)
    b, _ = await _member(make_client)
    a_file = (await _upload(a)).json()["file_id"]
    tid = (await _file(b))["id"]
    stolen = await b.post(
        f"/api/v1/support/tickets/{tid}/messages", json={"text": "x", "attachment_ids": [a_file]}
    )
    assert stolen.status_code == 400

    many = [(await _upload(b)).json()["file_id"] for _ in range(6)]
    too_many = await b.post(
        f"/api/v1/support/tickets/{tid}/messages", json={"text": "x", "attachment_ids": many}
    )
    assert too_many.status_code == 422  # rejected by the request model (max 5)


def test_signed_link_is_real_signing_not_a_plain_url(monkeypatch):
    """The link builder needs no network; it must carry a signature and expiry."""
    monkeypatch.setattr(storage.settings, "CLOUDINARY_CLOUD_NAME", "demo")
    monkeypatch.setattr(storage.settings, "CLOUDINARY_API_KEY", "key")
    monkeypatch.setattr(storage.settings, "CLOUDINARY_API_SECRET", "secret")
    monkeypatch.setattr(storage, "_configured", False)
    url = storage.signed_private_url("recast/private/support/ws/abc.png", 120)
    assert "signature=" in url and "expires_at=" in url


# ── Limits ───────────────────────────────────────────────────────────────────
async def test_ticket_rate_limit_per_member(make_client):
    member, _ = await _member(make_client)
    for c in ["Publishing", "Billing", "Brand voice", "Question", "Other"]:
        assert (await member.post("/api/v1/support/tickets", json={**TICKET, "category": c})).status_code == 200
    sixth = await member.post("/api/v1/support/tickets", json={**TICKET, "category": "Something else"})
    assert sixth.status_code == 429


async def test_duplicate_guard_offers_existing_ticket(make_client):
    member, _ = await _member(make_client)
    first = await _file(member)
    again = await member.post("/api/v1/support/tickets", json=TICKET)
    assert again.status_code == 409
    detail = again.json()["detail"]
    assert detail["code"] == "possible_duplicate"
    assert detail["existing_ticket_id"] == first["id"]
    forced = await member.post("/api/v1/support/tickets", json={**TICKET, "allow_duplicate": True})
    assert forced.status_code == 200


async def test_message_length_and_hourly_limit(make_client):
    member, _ = await _member(make_client)
    tid = (await _file(member))["id"]
    assert (await member.post(f"/api/v1/support/tickets/{tid}/messages", json={"text": "x" * 5001})).status_code == 422
    assert (await member.post(f"/api/v1/support/tickets/{tid}/messages", json={"text": "x" * 5000})).status_code == 200

    for i in range(rules.MAX_MESSAGES_PER_MEMBER_PER_HOUR - 1):
        res = await member.post(f"/api/v1/support/tickets/{tid}/messages", json={"text": f"m{i}"})
        assert res.status_code == 200, res.text
    limited = await member.post(f"/api/v1/support/tickets/{tid}/messages", json={"text": "one too many"})
    assert limited.status_code == 429


async def test_muted_member_cannot_file(make_client):
    member, user = await _member(make_client)
    await users.update_one({"id": user["id"]}, {"$set": {"support_muted": True}})
    assert (await member.post("/api/v1/support/tickets", json=TICKET)).status_code == 403


# ── Paging ───────────────────────────────────────────────────────────────────
async def test_long_threads_are_paged(make_client):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member))["id"]
    now = datetime.now(timezone.utc)
    extra = [
        {
            "sender": "staff" if i % 2 else "member", "sender_name": "x", "text": f"msg {i}",
            "created_at": now, "is_internal": False, "attachments": [],
        }
        for i in range(1, 130)
    ]
    await support_tickets.update_one({"id": tid}, {"$push": {"messages": {"$each": extra}}})

    page = (await member.get(f"/api/v1/support/tickets/{tid}")).json()
    assert len(page["messages"]) == rules.MESSAGE_PAGE_SIZE
    assert page["message_total"] == 130
    assert page["has_older"] is True
    assert page["messages"][-1]["text"] == "msg 129"

    older = (await member.get(f"/api/v1/support/tickets/{tid}", params={"before": page["first_index"]})).json()
    assert older["messages"][-1]["text"] == f"msg {page['first_index'] - 1}"
    oldest = (await member.get(f"/api/v1/support/tickets/{tid}", params={"before": 5, "limit": 50})).json()
    assert oldest["has_older"] is False and len(oldest["messages"]) == 5

    ops = (await staff.get(f"/api/v1/ops/support/tickets/{tid}", params={"limit": 20})).json()
    assert len(ops["ticket"]["messages"]) == 20 and ops["has_older"] is True

    listed = (await member.get("/api/v1/support/tickets")).json()["tickets"][0]
    assert len(listed["messages"]) == 1  # the list only carries the preview


async def test_member_list_paginates(make_client):
    member, _ = await _member(make_client)
    for c in ("Publishing", "Billing", "Brand voice"):
        await member.post("/api/v1/support/tickets", json={**TICKET, "category": c})
    page = (await member.get("/api/v1/support/tickets", params={"limit": 2})).json()
    assert len(page["tickets"]) == 2 and page["total"] == 3
    rest = (await member.get("/api/v1/support/tickets", params={"limit": 2, "skip": 2})).json()
    assert len(rest["tickets"]) == 1


# ── Ops search and filters ───────────────────────────────────────────────────
async def test_ops_search_and_filters(make_client):
    member, user = await _member(make_client)
    staff, _ = await _staff(make_client)
    platform = f"plat{uuid4().hex[:8]}"  # unique, so tickets from other tests never match
    made = await _file(
        member,
        subject="Weekly report",
        description="The chart on performance is blank",
        source_context={"type": "platform", "id": platform},
    )
    tid = made["id"]
    await _file(member, subject="Other thing", category="Billing", description="Invoice question")
    await support_tickets.update_one({"id": tid}, {"$set": {"workspace_tier": "duo"}})

    async def ids(**params):
        return [t["id"] for t in (await staff.get("/api/v1/ops/support/tickets", params=params)).json()["tickets"]]

    assert await ids(q="chart on performance") == [tid]
    assert tid in await ids(q=user["email"][:12])
    assert await ids(platform=platform) == [tid]
    assert await ids(platform="instagram") == []
    assert await ids(plan="duo") == [tid]
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert tid in await ids(created_from=yesterday)
    assert await ids(created_to=yesterday) == []
    assert tid in await ids(unread="true")


# ── Snooze and the lifecycle scheduler ───────────────────────────────────────
async def test_snooze_hides_then_wakes(make_client):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member))["id"]
    await staff.post(f"/api/v1/ops/support/tickets/{tid}/claim")

    later = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    assert (await staff.patch(f"/api/v1/ops/support/tickets/{tid}", json={"snoozed_until": later})).status_code == 200
    past = await staff.patch(
        f"/api/v1/ops/support/tickets/{tid}", json={"snoozed_until": "2020-01-01T00:00:00+00:00"}
    )
    assert past.status_code == 400

    default = [t["id"] for t in (await staff.get("/api/v1/ops/support/tickets")).json()["tickets"]]
    snoozed = [
        t["id"] for t in (await staff.get("/api/v1/ops/support/tickets", params={"view": "snoozed"})).json()["tickets"]
    ]
    assert tid not in default and tid in snoozed

    await staff.post("/api/v1/ops/support/notifications/read")
    done = await run_support_lifecycle(datetime.now(timezone.utc) + timedelta(hours=3))
    assert done["woken"] == 1
    assert tid in [t["id"] for t in (await staff.get("/api/v1/ops/support/tickets")).json()["tickets"]]
    assert (await staff.get("/api/v1/ops/support/notifications")).json()["notifications"][0]["type"] == "snooze_over"

    await staff.patch(f"/api/v1/ops/support/tickets/{tid}", json={"snoozed_until": later})
    await staff.patch(f"/api/v1/ops/support/tickets/{tid}", json={"clear_snooze": True})
    assert tid in [t["id"] for t in (await staff.get("/api/v1/ops/support/tickets")).json()["tickets"]]


async def test_lifecycle_reminder_auto_resolve_and_auto_close(make_client):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member))["id"]
    await staff.post(
        f"/api/v1/ops/support/tickets/{tid}/messages",
        json={"text": "Which account?", "set_status": "waiting_on_member"},
    )
    base = datetime.now(timezone.utc)
    await member.post("/api/v1/support/notifications/read")

    assert not any((await run_support_lifecycle(base + timedelta(hours=1))).values())

    first = await run_support_lifecycle(base + timedelta(days=3, hours=1))
    assert first["reminded"] == 1
    assert (await run_support_lifecycle(base + timedelta(days=4)))["reminded"] == 0
    types = [n["type"] for n in (await member.get("/api/v1/support/notifications")).json()["notifications"]]
    assert "ticket_reminder" in types

    second = await run_support_lifecycle(base + timedelta(days=7, hours=1))
    assert second["auto_resolved"] == 1
    assert (await member.get(f"/api/v1/support/tickets/{tid}")).json()["status"] == "resolved"

    resolved_at = (await support_tickets_doc(tid))["resolved_at"]
    if resolved_at.tzinfo is None:
        resolved_at = resolved_at.replace(tzinfo=timezone.utc)
    assert (await run_support_lifecycle(resolved_at + timedelta(days=5, hours=1)))["close_warned"] == 1
    assert (await run_support_lifecycle(resolved_at + timedelta(days=7, hours=1)))["auto_closed"] == 1
    final = (await member.get(f"/api/v1/support/tickets/{tid}")).json()
    assert final["status"] == "closed" and final["closed_reason"] == "auto_closed"
    events = [e["type"] for e in (await staff.get(f"/api/v1/ops/support/tickets/{tid}")).json()["events"]]
    assert "auto_resolved" in events and "auto_closed" in events


async def test_member_reply_resets_the_waiting_clock(make_client):
    member, _ = await _member(make_client)
    staff, _ = await _staff(make_client)
    tid = (await _file(member))["id"]
    await staff.post(
        f"/api/v1/ops/support/tickets/{tid}/messages",
        json={"text": "Which account?", "set_status": "waiting_on_member"},
    )
    base = datetime.now(timezone.utc)
    await run_support_lifecycle(base + timedelta(days=3, hours=1))
    await member.post(f"/api/v1/support/tickets/{tid}/messages", json={"text": "The LinkedIn one."})
    ticket = await support_tickets_doc(tid)
    assert ticket["status"] == "investigating" and ticket["reminder_sent_at"] is None


# ── Workspace-admin visibility ───────────────────────────────────────────────
async def test_workspace_admin_can_read_team_tickets_only_when_enabled(make_client):
    owner = make_client()
    await signup_new_user(owner, name="Owner")
    ws_id = await create_workspace(owner, "Team WS", tier="large")
    admin, _ = await invite_and_accept(owner, make_client, ws_id, "admin")
    editor, _ = await invite_and_accept(owner, make_client, ws_id, "editor")
    hdr = {"X-Workspace-Id": ws_id}

    filed = await editor.post("/api/v1/support/tickets", json=TICKET, headers=hdr)
    assert filed.status_code == 200, filed.text
    tid = filed.json()["id"]

    # Off by default: an admin sees nothing of a teammate's ticket.
    assert (await admin.get(f"/api/v1/support/tickets/{tid}", headers=hdr)).status_code == 404
    assert (await admin.get("/api/v1/support/tickets", params={"scope": "workspace"}, headers=hdr)).status_code == 403

    # Only the owner can turn it on.
    off = await admin.put("/api/v1/support/settings", json={"admins_can_view_tickets": True}, headers=hdr)
    assert off.status_code == 403
    on = await owner.put("/api/v1/support/settings", json={"admins_can_view_tickets": True}, headers=hdr)
    assert on.status_code == 200

    seen = await admin.get(f"/api/v1/support/tickets/{tid}", headers=hdr)
    assert seen.status_code == 200 and seen.json()["is_own"] is False
    listed = (await admin.get("/api/v1/support/tickets", params={"scope": "workspace"}, headers=hdr)).json()
    assert tid in [t["id"] for t in listed["tickets"]]

    # Read only, and an editor still sees only their own.
    assert (
        await admin.post(f"/api/v1/support/tickets/{tid}/messages", json={"text": "hi"}, headers=hdr)
    ).status_code == 404
    assert (await admin.post(f"/api/v1/support/tickets/{tid}/close", json={}, headers=hdr)).status_code == 404
    other_editor, _ = await invite_and_accept(owner, make_client, ws_id, "editor")
    assert (await other_editor.get(f"/api/v1/support/tickets/{tid}", headers=hdr)).status_code == 404
